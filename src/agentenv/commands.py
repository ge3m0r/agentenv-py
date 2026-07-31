from __future__ import annotations

import base64
import binascii
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .models import Sandbox

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class CommandServiceError(RuntimeError):
    """Base error for managed command operations."""


class CommandNotFoundError(CommandServiceError):
    """Raised when a managed command cannot be found."""


class CommandConflictError(CommandServiceError):
    """Raised when a command operation conflicts with its current state."""


class CommandUnavailableError(CommandServiceError):
    """Raised when a backend has no managed command data plane."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ManagedCommand:
    id: str
    sandbox_id: str
    command: str
    pid: int
    state: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _CommandSession:
    def __init__(
        self,
        info: ManagedCommand,
        process: subprocess.Popen[bytes],
        on_exit: Any,
    ):
        self.info = info
        self.process = process
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.condition = threading.Condition()
        self.completed = threading.Event()
        self._on_exit = on_exit
        self._started_monotonic = time.monotonic()
        self._stdout_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stdout, self.stdout),
            name=f"{info.id}-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stderr, self.stderr),
            name=f"{info.id}-stderr",
            daemon=True,
        )
        self._waiter_thread = threading.Thread(
            target=self._wait_for_exit,
            name=f"{info.id}-waiter",
            daemon=True,
        )

    def start(self) -> None:
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._waiter_thread.start()

    def _read_stream(self, stream: Any, destination: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            with self.condition:
                destination.extend(chunk)
                self.condition.notify_all()

    def _wait_for_exit(self) -> None:
        exit_code = self.process.wait()
        self._stdout_thread.join()
        self._stderr_thread.join()
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None:
                stream.close()
        finished = time.monotonic()
        with self.condition:
            self.info.exit_code = exit_code
            self.info.state = "exited"
            self.info.finished_at = _now()
            self.info.duration_ms = round((finished - self._started_monotonic) * 1000)
            self.condition.notify_all()
        try:
            self._on_exit(self)
        finally:
            with self.condition:
                self.completed.set()
                self.condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        return self.completed.wait(timeout)

    def set_state(self, state: str) -> None:
        with self.condition:
            if not self.completed.is_set():
                self.info.state = state
                self.condition.notify_all()

    def output(
        self,
        stdout_offset: int,
        stderr_offset: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        if stdout_offset < 0 or stderr_offset < 0:
            raise CommandServiceError("output offsets cannot be negative")
        with self.condition:
            if stdout_offset > len(self.stdout) or stderr_offset > len(self.stderr):
                raise CommandServiceError("output offset is beyond available data")
            if (
                stdout_offset == len(self.stdout)
                and stderr_offset == len(self.stderr)
                and not self.completed.is_set()
                and wait_seconds > 0
            ):
                self.condition.wait(wait_seconds)
            stdout = bytes(self.stdout[stdout_offset:])
            stderr = bytes(self.stderr[stderr_offset:])
            return {
                "command": self.info.to_dict(),
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "stdoutBase64": base64.b64encode(stdout).decode("ascii"),
                "stderrBase64": base64.b64encode(stderr).decode("ascii"),
                "next": {
                    "stdout": len(self.stdout),
                    "stderr": len(self.stderr),
                },
            }


class CommandService:
    """In-process command controller with reconnectable output buffers."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        self._sessions: dict[str, _CommandSession] = {}
        self._lock = threading.RLock()

    def start(self, sandbox_id: str, command: str) -> ManagedCommand:
        if (
            not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
        ):
            raise CommandServiceError("command must be a non-empty string")
        sandbox = self._running_sandbox(sandbox_id)
        command_id = f"cmd_{uuid4().hex[:12]}"
        try:
            process, pid = self.orchestrator.backend.start_managed_command(
                sandbox, command_id, command
            )
        except NotImplementedError as error:
            raise CommandUnavailableError(
                f"backend {sandbox.backend} does not support managed commands"
            ) from error
        info = ManagedCommand(
            id=command_id,
            sandbox_id=sandbox.id,
            command=command,
            pid=pid,
            state="running",
            started_at=_now(),
        )
        session = _CommandSession(info, process, self._command_exited)
        with self._lock:
            self._sessions[command_id] = session
        self.orchestrator._event(
            "command_started",
            "command",
            command_id,
            sandbox_id=sandbox.id,
            command=command,
            pid=pid,
        )
        session.start()
        return info

    def list(self, sandbox_id: str) -> list[ManagedCommand]:
        self.orchestrator.get_sandbox(sandbox_id)
        with self._lock:
            return [
                session.info
                for session in self._sessions.values()
                if session.info.sandbox_id == sandbox_id
            ]

    def connect(
        self,
        sandbox_id: str,
        *,
        command_id: str | None = None,
        pid: int | None = None,
    ) -> ManagedCommand:
        if command_id is None and pid is None:
            raise CommandServiceError("provide command_id or pid")
        if command_id is not None:
            return self._session(sandbox_id, command_id).info
        with self._lock:
            session = next(
                (
                    item
                    for item in self._sessions.values()
                    if item.info.sandbox_id == sandbox_id
                    and item.info.pid == pid
                ),
                None,
            )
        if session is None:
            raise CommandNotFoundError(
                f"command not found in sandbox {sandbox_id}: pid {pid}"
            )
        return session.info

    def get(self, sandbox_id: str, command_id: str) -> ManagedCommand:
        return self._session(sandbox_id, command_id).info

    def read_output(
        self,
        sandbox_id: str,
        command_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        if wait_seconds < 0 or wait_seconds > 30:
            raise CommandServiceError("wait_seconds must be between 0 and 30")
        return self._session(sandbox_id, command_id).output(
            stdout_offset, stderr_offset, wait_seconds
        )

    def send_stdin(
        self,
        sandbox_id: str,
        command_id: str,
        data: str,
        *,
        encoding: str = "utf-8",
    ) -> ManagedCommand:
        session = self._session(sandbox_id, command_id)
        if session.info.state != "running":
            raise CommandConflictError(
                f"stdin requires a running command; state is {session.info.state}"
            )
        if encoding == "utf-8":
            content = data.encode("utf-8")
        elif encoding == "base64":
            try:
                content = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as error:
                raise CommandServiceError("data is not valid base64") from error
        else:
            raise CommandServiceError("encoding must be utf-8 or base64")
        if session.process.stdin is None:
            raise CommandConflictError("command stdin is not available")
        try:
            session.process.stdin.write(content)
            session.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise CommandConflictError("command stdin is closed") from error
        return session.info

    def signal(
        self, sandbox_id: str, command_id: str, signal_name: str
    ) -> ManagedCommand:
        signal_name = signal_name.upper()
        if signal_name.startswith("SIG"):
            signal_name = signal_name[3:]
        if signal_name not in {"TERM", "KILL", "INT"}:
            raise CommandServiceError("signal must be TERM, KILL or INT")
        session = self._session(sandbox_id, command_id)
        if session.completed.is_set():
            return session.info
        sandbox = self.orchestrator.get_sandbox(sandbox_id)
        self.orchestrator.backend.signal_managed_command(
            sandbox, command_id, session.process, signal_name
        )
        return session.info

    def wait(
        self,
        sandbox_id: str,
        command_id: str,
        timeout: float | None = None,
    ) -> ManagedCommand:
        if timeout is not None and timeout < 0:
            raise CommandServiceError("timeout cannot be negative")
        session = self._session(sandbox_id, command_id)
        if not session.wait(timeout):
            session.info.timed_out = True
            self.signal(sandbox_id, command_id, "KILL")
            if not session.wait(2):
                raise CommandConflictError(
                    f"command did not exit after timeout: {command_id}"
                )
        return session.info

    def pause_sandbox(self, sandbox: Sandbox) -> None:
        for session in self._active_sessions(sandbox.id):
            self.orchestrator.backend.pause_managed_command(
                sandbox, session.process
            )
            session.set_state("paused")

    def resume_sandbox(self, sandbox: Sandbox) -> None:
        for session in self._active_sessions(sandbox.id):
            self.orchestrator.backend.resume_managed_command(
                sandbox, session.process
            )
            session.set_state("running")

    def terminate_sandbox(self, sandbox: Sandbox) -> None:
        sessions = self._active_sessions(sandbox.id)
        for session in sessions:
            try:
                self.orchestrator.backend.signal_managed_command(
                    sandbox, session.info.id, session.process, "KILL"
                )
            except Exception:
                try:
                    session.process.kill()
                except OSError:
                    pass
        for session in sessions:
            session.wait(2)
        with self._lock:
            for command_id in [
                item.info.id
                for item in self._sessions.values()
                if item.info.sandbox_id == sandbox.id
            ]:
                self._sessions.pop(command_id, None)

    def _running_sandbox(self, sandbox_id: str) -> Sandbox:
        sandbox = self.orchestrator.get_sandbox(sandbox_id)
        self.orchestrator.ensure_backend(sandbox)
        return self.orchestrator.prepare_activity(sandbox, "commands")

    def _session(self, sandbox_id: str, command_id: str) -> _CommandSession:
        self.orchestrator.get_sandbox(sandbox_id)
        with self._lock:
            session = self._sessions.get(command_id)
        if session is None or session.info.sandbox_id != sandbox_id:
            raise CommandNotFoundError(f"command not found: {command_id}")
        return session

    def _active_sessions(self, sandbox_id: str) -> list[_CommandSession]:
        with self._lock:
            return [
                session
                for session in self._sessions.values()
                if session.info.sandbox_id == sandbox_id
                and not session.completed.is_set()
            ]

    def _command_exited(self, session: _CommandSession) -> None:
        sandbox = self.orchestrator.store.get_sandbox(session.info.sandbox_id)
        if sandbox and sandbox.backend == self.orchestrator.backend.name:
            self.orchestrator.backend.cleanup_managed_command(
                sandbox, session.info.id
            )
        self.orchestrator._event(
            "command_exited",
            "command",
            session.info.id,
            sandbox_id=session.info.sandbox_id,
            exit_code=session.info.exit_code,
            duration_ms=session.info.duration_ms,
            timed_out=session.info.timed_out,
        )
