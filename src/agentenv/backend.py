from __future__ import annotations

import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .models import (
    CommandResult,
    NetworkPolicy,
    ResourceLimits,
    Sandbox,
    Snapshot,
    Template,
)
from .oci import DockerImageResolver


class SandboxBackend(ABC):
    """The replaceable runtime boundary; Firecracker can be added behind this."""

    name = "abstract"

    def prepare_template(self, template: Template) -> Template:
        return template

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


class DockerSandboxBackend(SandboxBackend):
    """
    Docker CLI backend with persistent containers and a bind-mounted workspace.

    CPU, memory and PID limits are enforced by Docker. disk_size_mb is recorded
    for API compatibility but cannot be portably enforced for bind mounts.
    """

    name = "docker"

    def __init__(
        self,
        docker_binary: str = "docker",
        pull_missing: bool = True,
        runner: DockerRunner = subprocess.run,
    ):
        self.docker_binary = docker_binary
        self.runner = runner
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
