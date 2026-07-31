from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .models import (
    CommandResult,
    NetworkPolicy,
    ResourceLimits,
    Sandbox,
    Snapshot,
    Template,
)
from .oci import DockerImageResolver
from .filesystem import FilesystemUnavailableError, WorkspaceFilesystem


@dataclass
class LocalPtyHandle:
    """Opaque handle for a local PTY: master fd for I/O + child pid for kill."""

    master_fd: int
    pid: int


class SandboxBackend(ABC):
    """The replaceable runtime boundary; Firecracker can be added behind this."""

    name = "abstract"

    def prepare_template(self, template: Template) -> Template:
        return template

    def filesystem(self, sandbox: Sandbox) -> WorkspaceFilesystem:
        raise FilesystemUnavailableError(
            f"backend {self.name} does not provide a filesystem data plane"
        )

    def start_managed_command(
        self, sandbox: Sandbox, command_id: str, command: str
    ) -> tuple[subprocess.Popen[bytes], int]:
        raise NotImplementedError

    def signal_managed_command(
        self,
        sandbox: Sandbox,
        command_id: str,
        process: subprocess.Popen[bytes],
        signal_name: str,
    ) -> None:
        raise NotImplementedError

    def pause_managed_command(
        self, sandbox: Sandbox, process: subprocess.Popen[bytes]
    ) -> None:
        return None

    def resume_managed_command(
        self, sandbox: Sandbox, process: subprocess.Popen[bytes]
    ) -> None:
        return None

    def cleanup_managed_command(
        self, sandbox: Sandbox, command_id: str
    ) -> None:
        return None

    def start_pty(
        self,
        sandbox: Sandbox,
        pty_id: str,
        *,
        rows: int,
        cols: int,
        command: str | None,
        cwd: str | None,
        envs: dict[str, str] | None,
        on_output: "Callable[[bytes], None]",
        on_exit: "Callable[[int], None]",
    ) -> tuple[Any, int]:
        raise NotImplementedError

    def send_pty_input(
        self, sandbox: Sandbox, handle: Any, data: bytes
    ) -> None:
        raise NotImplementedError

    def resize_pty(
        self, sandbox: Sandbox, handle: Any, rows: int, cols: int
    ) -> None:
        raise NotImplementedError

    def kill_pty(self, sandbox: Sandbox, handle: Any) -> None:
        raise NotImplementedError

    def cleanup_pty(self, sandbox: Sandbox, pty_id: str) -> None:
        return None

    @abstractmethod
    def create(self, template: Template, sandbox: Sandbox) -> None: ...

    @abstractmethod
    def execute(
        self, sandbox: Sandbox, command: str, timeout: float | None = None
    ) -> CommandResult: ...

    @abstractmethod
    def pause(self, sandbox: Sandbox) -> None: ...

    @abstractmethod
    def resume(self, sandbox: Sandbox) -> None: ...

    @abstractmethod
    def capture(self, sandbox: Sandbox, destination: Path) -> None: ...

    @abstractmethod
    def restore(self, snapshot: Snapshot, sandbox: Sandbox) -> None: ...

    def delete_snapshot(self, snapshot: Snapshot) -> None:
        """Release any backend-side snapshot resources.

        Default is a no-op (local/docker snapshots live entirely on disk and
        are removed by the orchestrator). Cloud backends override this to
        delete the remote snapshot.
        """
        return None

    @abstractmethod
    def update_network(self, sandbox: Sandbox, policy: NetworkPolicy) -> None: ...

    @abstractmethod
    def update_resources(
        self, sandbox: Sandbox, resources: ResourceLimits
    ) -> None: ...

    @abstractmethod
    def runtime_alive(self, sandbox: Sandbox) -> bool: ...

    @abstractmethod
    def destroy(self, sandbox: Sandbox) -> None: ...


class LocalProcessBackend(SandboxBackend):
    """
    Development backend using a copied directory and local subprocesses.

    It demonstrates the lifecycle but is NOT a security sandbox: commands run
    with the current user's permissions.
    """

    name = "local"

    def filesystem(self, sandbox: Sandbox) -> WorkspaceFilesystem:
        return WorkspaceFilesystem(sandbox.workspace_path)

    def start_managed_command(
        self, sandbox: Sandbox, command_id: str, command: str
    ) -> tuple[subprocess.Popen[bytes], int]:
        root = Path(sandbox.workspace_path).resolve()
        working_directory = (root / sandbox.workdir).resolve()
        if root != working_directory and root not in working_directory.parents:
            raise ValueError("workdir must stay inside the sandbox workspace")
        working_directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(sandbox.env)
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=working_directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return process, process.pid

    def signal_managed_command(
        self,
        sandbox: Sandbox,
        command_id: str,
        process: subprocess.Popen[bytes],
        signal_name: str,
    ) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, getattr(signal, f"SIG{signal_name}"))
            except ProcessLookupError:
                pass

    def pause_managed_command(
        self, sandbox: Sandbox, process: subprocess.Popen[bytes]
    ) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass

    def resume_managed_command(
        self, sandbox: Sandbox, process: subprocess.Popen[bytes]
    ) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def start_pty(
        self,
        sandbox: Sandbox,
        pty_id: str,
        *,
        rows: int,
        cols: int,
        command: str | None,
        cwd: str | None,
        envs: dict[str, str] | None,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> tuple["LocalPtyHandle", int]:
        if sys.platform == "win32":
            raise NotImplementedError(
                "local PTY requires a Unix-like system; use the docker or e2b backend"
            )
        import fcntl
        import pty as _pty
        import struct
        import termios

        root = Path(sandbox.workspace_path).resolve()
        working_directory = (root / (cwd or sandbox.workdir)).resolve()
        if root != working_directory and root not in working_directory.parents:
            raise ValueError("workdir must stay inside the sandbox workspace")
        working_directory.mkdir(parents=True, exist_ok=True)

        master_fd, slave_fd = _pty.openpty()
        self._set_pty_size(master_fd, rows, cols, fcntl, termios, struct)
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                os.close(master_fd)
                environment = os.environ.copy()
                environment.update(envs or {})
                environment.update(sandbox.env)
                os.chdir(working_directory)
                shell = environment.get("SHELL", "/bin/sh")
                argv = (
                    [shell, "-l", "-c", command] if command else [shell, "-l"]
                )
                os.execvpe(shell, argv, environment)
            except Exception:
                os._exit(127)
        os.close(slave_fd)

        def reader() -> None:
            try:
                while True:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    on_output(data)
            finally:
                try:
                    _, status = os.waitpid(pid, 0)
                    if os.WIFEXITED(status):
                        code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        code = 128 + os.WTERMSIG(status)
                    else:
                        code = -1
                except OSError:
                    code = -1
                on_exit(code)

        threading.Thread(
            target=reader, name=f"{pty_id}-reader", daemon=True
        ).start()
        return LocalPtyHandle(master_fd=master_fd, pid=pid), pid

    def send_pty_input(
        self, sandbox: Sandbox, handle: "LocalPtyHandle", data: bytes
    ) -> None:
        os.write(handle.master_fd, data)

    def resize_pty(
        self, sandbox: Sandbox, handle: "LocalPtyHandle", rows: int, cols: int
    ) -> None:
        import fcntl
        import struct
        import termios

        self._set_pty_size(handle.master_fd, rows, cols, fcntl, termios, struct)

    def kill_pty(self, sandbox: Sandbox, handle: "LocalPtyHandle") -> None:
        try:
            os.killpg(os.getpgid(handle.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def cleanup_pty(self, sandbox: Sandbox, pty_id: str) -> None:
        return None

    @staticmethod
    def _set_pty_size(fd: int, rows: int, cols: int, fcntl, termios, struct) -> None:
        try:
            fcntl.ioctl(
                fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    def create(self, template: Template, sandbox: Sandbox) -> None:
        source = Path(template.rootfs_path)
        destination = Path(sandbox.workspace_path)
        if destination.exists():
            raise FileExistsError(f"workspace already exists: {destination}")
        if source.exists():
            shutil.copytree(source, destination)
        else:
            destination.mkdir(parents=True)

    def execute(
        self, sandbox: Sandbox, command: str, timeout: float | None = None
    ) -> CommandResult:
        root = Path(sandbox.workspace_path).resolve()
        working_directory = (root / sandbox.workdir).resolve()
        if root != working_directory and root not in working_directory.parents:
            raise ValueError("workdir must stay inside the sandbox workspace")
        working_directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(sandbox.env)
        started = time.monotonic()
        executed_at = datetime.now(timezone.utc).isoformat()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=working_directory,
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            exit_code, stdout, stderr = (
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        except subprocess.TimeoutExpired as error:
            exit_code = 124
            stdout = error.stdout or ""
            stderr = error.stderr or f"command timed out after {timeout} seconds"
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round((time.monotonic() - started) * 1000),
            executed_at=executed_at,
        )

    def pause(self, sandbox: Sandbox) -> None:
        return None

    def resume(self, sandbox: Sandbox) -> None:
        return None

    def capture(self, sandbox: Sandbox, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(f"snapshot already exists: {destination}")
        shutil.copytree(sandbox.workspace_path, destination)

    def restore(self, snapshot: Snapshot, sandbox: Sandbox) -> None:
        destination = Path(sandbox.workspace_path)
        if destination.exists():
            raise FileExistsError(f"workspace already exists: {destination}")
        shutil.copytree(snapshot.rootfs_path, destination)

    def update_network(self, sandbox: Sandbox, policy: NetworkPolicy) -> None:
        return None

    def update_resources(
        self, sandbox: Sandbox, resources: ResourceLimits
    ) -> None:
        return None

    def runtime_alive(self, sandbox: Sandbox) -> bool:
        return Path(sandbox.workspace_path).exists()

    def destroy(self, sandbox: Sandbox) -> None:
        workspace = Path(sandbox.workspace_path)
        if workspace.exists():
            shutil.rmtree(workspace)


class DockerBackendError(RuntimeError):
    pass


DockerRunner = Callable[..., subprocess.CompletedProcess[str]]
PopenFactory = Callable[..., subprocess.Popen[bytes]]


class DockerSandboxBackend(SandboxBackend):
    """
    Docker CLI backend with persistent containers and a bind-mounted workspace.

    CPU, memory and PID limits are enforced by Docker. disk_size_mb is recorded
    for API compatibility but cannot be portably enforced for bind mounts.
    """

    name = "docker"

    def filesystem(self, sandbox: Sandbox) -> WorkspaceFilesystem:
        # /workspace is a bind mount of workspace_path, so host-side file
        # operations and commands running in the container see the same data.
        return WorkspaceFilesystem(sandbox.workspace_path)

    def start_managed_command(
        self, sandbox: Sandbox, command_id: str, command: str
    ) -> tuple[subprocess.Popen[bytes], int]:
        pid_directory = Path(sandbox.workspace_path) / ".agentenv" / "commands"
        pid_directory.mkdir(parents=True, exist_ok=True)
        host_pid_file = pid_directory / f"{command_id}.pid"
        container_pid_file = f"/workspace/.agentenv/commands/{command_id}.pid"
        arguments = [
            self.docker_binary,
            "exec",
            "-i",
            "-w",
            self._container_workdir(sandbox),
        ]
        for key, value in sandbox.env.items():
            arguments.extend(["-e", f"{key}={value}"])
        arguments.extend(
            [
                "-e",
                f"AGENTENV_COMMAND={command}",
                "-e",
                f"AGENTENV_PID_FILE={container_pid_file}",
                self._runtime_id(sandbox),
                "/bin/sh",
                "-lc",
                'echo "$$" > "$AGENTENV_PID_FILE" && '
                'exec /bin/sh -lc "$AGENTENV_COMMAND"',
            ]
        )
        process = self.popen_factory(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        runtime_pid = process.pid
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                runtime_pid = int(host_pid_file.read_text().strip())
                break
            except (FileNotFoundError, ValueError):
                if process.poll() is not None:
                    break
                time.sleep(0.01)
        return process, runtime_pid

    def signal_managed_command(
        self,
        sandbox: Sandbox,
        command_id: str,
        process: subprocess.Popen[bytes],
        signal_name: str,
    ) -> None:
        pid_file = (
            Path(sandbox.workspace_path)
            / ".agentenv"
            / "commands"
            / f"{command_id}.pid"
        )
        try:
            runtime_pid = int(pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            if process.poll() is None:
                os.killpg(process.pid, getattr(signal, f"SIG{signal_name}"))
            return
        self._run(
            [
                "exec",
                self._runtime_id(sandbox),
                "kill",
                f"-{signal_name}",
                str(runtime_pid),
            ],
            allow_failure=True,
        )

    def cleanup_managed_command(
        self, sandbox: Sandbox, command_id: str
    ) -> None:
        pid_file = (
            Path(sandbox.workspace_path)
            / ".agentenv"
            / "commands"
            / f"{command_id}.pid"
        )
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass

    def start_pty(
        self,
        sandbox: Sandbox,
        pty_id: str,
        *,
        rows: int,
        cols: int,
        command: str | None,
        cwd: str | None,
        envs: dict[str, str] | None,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> tuple[subprocess.Popen[bytes], int]:
        arguments = [
            self.docker_binary,
            "exec",
            "-i",
            "-t",
            "-w",
            self._container_workdir(sandbox),
        ]
        for key, value in sandbox.env.items():
            arguments.extend(["-e", f"{key}={value}"])
        if envs:
            for key, value in envs.items():
                arguments.extend(["-e", f"{key}={value}"])
        arguments.append(self._runtime_id(sandbox))
        if command:
            arguments.extend(["/bin/sh", "-lc", command])
        else:
            arguments.extend(["/bin/sh", "-l"])
        process = self.popen_factory(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        def reader() -> None:
            try:
                stream = process.stdout
                while True:
                    data = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                    if not data:
                        break
                    on_output(data)
            finally:
                on_exit(process.wait())

        threading.Thread(
            target=reader, name=f"{pty_id}-reader", daemon=True
        ).start()
        return process, process.pid

    def send_pty_input(
        self, sandbox: Sandbox, handle: subprocess.Popen[bytes], data: bytes
    ) -> None:
        if handle.stdin is None:
            raise DockerBackendError("PTY stdin is not available")
        try:
            handle.stdin.write(data)
            handle.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise DockerBackendError("PTY stdin is closed") from error

    def resize_pty(
        self, sandbox: Sandbox, handle: subprocess.Popen[bytes], rows: int, cols: int
    ) -> None:
        # The Docker CLI does not expose resizing a `docker exec` PTY after it
        # has started; the container API endpoint is not reachable via CLI.
        raise NotImplementedError(
            "docker PTY cannot be resized after start via the Docker CLI"
        )

    def kill_pty(self, sandbox: Sandbox, handle: subprocess.Popen[bytes]) -> None:
        if handle.poll() is None:
            handle.kill()

    def cleanup_pty(self, sandbox: Sandbox, pty_id: str) -> None:
        return None

    def __init__(
        self,
        docker_binary: str = "docker",
        pull_missing: bool = True,
        runner: DockerRunner = subprocess.run,
        popen_factory: PopenFactory = subprocess.Popen,
    ):
        self.docker_binary = docker_binary
        self.runner = runner
        self.popen_factory = popen_factory
        self.resolver = DockerImageResolver(
            docker_binary=docker_binary,
            pull_missing=pull_missing,
            runner=runner,
        )

    def prepare_template(self, template: Template) -> Template:
        resolved = self.resolver.resolve(template.source)
        template.image_ref = resolved.reference.canonical
        template.image_digest = resolved.digest
        template.env = {**resolved.env, **template.env}
        return template

    def create(self, template: Template, sandbox: Sandbox) -> None:
        image = template.image_ref or template.source
        self._prepare_workspace(Path(template.rootfs_path), Path(sandbox.workspace_path))
        sandbox.image_ref = image
        sandbox.runtime_id = self._container_name(sandbox.id)
        try:
            self._create_container(sandbox)
        except Exception:
            self._remove_container(sandbox.runtime_id)
            shutil.rmtree(Path(sandbox.workspace_path).parent, ignore_errors=True)
            raise

    def restore(self, snapshot: Snapshot, sandbox: Sandbox) -> None:
        if not snapshot.image_ref:
            raise DockerBackendError("Docker snapshot is missing image_ref")
        self._prepare_workspace(
            Path(snapshot.rootfs_path), Path(sandbox.workspace_path)
        )
        sandbox.image_ref = snapshot.image_ref
        sandbox.runtime_id = self._container_name(sandbox.id)
        try:
            self._create_container(sandbox)
        except Exception:
            self._remove_container(sandbox.runtime_id)
            shutil.rmtree(Path(sandbox.workspace_path).parent, ignore_errors=True)
            raise

    def execute(
        self, sandbox: Sandbox, command: str, timeout: float | None = None
    ) -> CommandResult:
        runtime_id = self._runtime_id(sandbox)
        arguments = ["exec", "-w", self._container_workdir(sandbox)]
        for key, value in sandbox.env.items():
            arguments.extend(["-e", f"{key}={value}"])
        arguments.extend([runtime_id, "/bin/sh", "-lc", command])
        started = time.monotonic()
        executed_at = datetime.now(timezone.utc).isoformat()
        try:
            result = self.runner(
                [self.docker_binary, *arguments],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            exit_code, stdout, stderr = (
                result.returncode,
                result.stdout,
                result.stderr,
            )
        except subprocess.TimeoutExpired as error:
            exit_code = 124
            stdout = error.stdout or ""
            stderr = error.stderr or f"command timed out after {timeout} seconds"
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round((time.monotonic() - started) * 1000),
            executed_at=executed_at,
        )

    def pause(self, sandbox: Sandbox) -> None:
        self._run(["pause", self._runtime_id(sandbox)])

    def resume(self, sandbox: Sandbox) -> None:
        self._run(["unpause", self._runtime_id(sandbox)])

    def capture(self, sandbox: Sandbox, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(f"snapshot already exists: {destination}")
        shutil.copytree(sandbox.workspace_path, destination)

    def update_network(self, sandbox: Sandbox, policy: NetworkPolicy) -> None:
        runtime_id = self._runtime_id(sandbox)
        deny_all = not policy.allow_internet_access or "0.0.0.0/0" in policy.deny_out
        if deny_all:
            self._run(
                ["network", "disconnect", "-f", "bridge", runtime_id],
                allow_failure=True,
            )
        else:
            result = self._run(
                ["network", "connect", "bridge", runtime_id],
                allow_failure=True,
            )
            if result.returncode != 0 and "already exists" not in result.stderr.lower():
                raise DockerBackendError(result.stderr.strip())

    def update_resources(
        self, sandbox: Sandbox, resources: ResourceLimits
    ) -> None:
        self._run(
            [
                "update",
                "--cpus",
                str(resources.cpu_count),
                "--memory",
                f"{resources.memory_mb}m",
                "--pids-limit",
                str(resources.pids_limit),
                self._runtime_id(sandbox),
            ]
        )

    def runtime_alive(self, sandbox: Sandbox) -> bool:
        if not sandbox.runtime_id:
            return False
        result = self._run(
            [
                "inspect",
                "--format",
                "{{.State.Running}}",
                sandbox.runtime_id,
            ],
            allow_failure=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def destroy(self, sandbox: Sandbox) -> None:
        if sandbox.runtime_id:
            self._remove_container(sandbox.runtime_id)
        workspace = Path(sandbox.workspace_path).parent
        if workspace.exists():
            shutil.rmtree(workspace)

    def _create_container(self, sandbox: Sandbox) -> None:
        resources = sandbox.resources
        resources.validate()
        arguments = [
            "create",
            "--name",
            self._runtime_id(sandbox),
            "--label",
            f"agentenv.sandbox.id={sandbox.id}",
            "--label",
            f"agentenv.disk-size-mb={resources.disk_size_mb}",
            "--cpus",
            str(resources.cpu_count),
            "--memory",
            f"{resources.memory_mb}m",
            "--pids-limit",
            str(resources.pids_limit),
            "--network",
            self._initial_network(sandbox.network),
            "--volume",
            f"{Path(sandbox.workspace_path).resolve()}:/workspace",
            "--workdir",
            self._container_workdir(sandbox),
            "--init",
        ]
        for key, value in sandbox.env.items():
            arguments.extend(["--env", f"{key}={value}"])
        arguments.extend(
            [
                sandbox.image_ref or "",
                "/bin/sh",
                "-lc",
                "trap : TERM INT; while :; do sleep 3600; done",
            ]
        )
        self._run(arguments)
        self._run(["start", self._runtime_id(sandbox)])

    def _prepare_workspace(self, source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(f"workspace already exists: {destination}")
        if source.exists():
            shutil.copytree(source, destination)
        else:
            destination.mkdir(parents=True)

    def _container_workdir(self, sandbox: Sandbox) -> str:
        workdir = PurePosixPath(sandbox.workdir)
        if workdir.is_absolute():
            if workdir == PurePosixPath("/workspace"):
                return "/workspace"
            try:
                relative = workdir.relative_to("/workspace")
            except ValueError as error:
                raise DockerBackendError("Docker workdir must be under /workspace") from error
        else:
            relative = workdir
        if ".." in relative.parts:
            raise DockerBackendError("workdir cannot leave /workspace")
        return str(PurePosixPath("/workspace") / relative)

    def _initial_network(self, policy: NetworkPolicy) -> str:
        if not policy.allow_internet_access or "0.0.0.0/0" in policy.deny_out:
            return "none"
        return "bridge"

    def _runtime_id(self, sandbox: Sandbox) -> str:
        if not sandbox.runtime_id:
            raise DockerBackendError(f"sandbox {sandbox.id} has no Docker runtime ID")
        return sandbox.runtime_id

    def _container_name(self, sandbox_id: str) -> str:
        return f"agentenv-{sandbox_id}"

    def _remove_container(self, runtime_id: str | None) -> None:
        if runtime_id:
            self._run(["rm", "-f", runtime_id], allow_failure=True)

    def _run(
        self, arguments: list[str], allow_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            [self.docker_binary, *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 and not allow_failure:
            raise DockerBackendError(
                result.stderr.strip()
                or f"docker {' '.join(arguments[:2])} failed with {result.returncode}"
            )
        return result
