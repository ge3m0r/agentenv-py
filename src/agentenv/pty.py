"""Interactive PTY (pseudo-terminal) data plane.

Mirrors :mod:`agentenv.commands`: an in-process controller with reconnectable
output buffers, backed by per-backend PTY primitives. Each backend owns the
reader thread that pushes PTY bytes into the session buffer via callbacks, so
the service stays backend-agnostic.

A WebSocket transport (:mod:`agentenv.pty_ws`) turns a session into a live,
bidirectional terminal: binary frames carry raw input/output bytes, text frames
carry JSON control messages (resize/kill), and clients can reconnect by
reopening the socket with ``?offset=`` to catch up on buffered output.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from .models import Sandbox

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class PtyServiceError(RuntimeError):
    """Base error for managed PTY operations."""


class PtyNotFoundError(PtyServiceError):
    """Raised when a managed PTY cannot be found."""


class PtyConflictError(PtyServiceError):
    """Raised when a PTY operation conflicts with its current state."""


class PtyUnavailableError(PtyServiceError):
    """Raised when a backend has no PTY data plane or lacks a capability."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ManagedPty:
    id: str
    sandbox_id: str
    pid: int
    rows: int
    cols: int
    command: str | None
    state: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _PtySession:
    """A live PTY handle plus a reconnectable output buffer.

    The backend's reader thread calls :meth:`append_output` as bytes arrive and
    :meth:`handle_exit` when the process ends. Clients read output via
    :meth:`read_output`, which blocks briefly for new data when at the buffer
    end — the same pattern used by :class:`agentenv.commands._CommandSession`.
    """

    def __init__(
        self,
        info: ManagedPty,
        handle: Any,
        backend: Any,
        sandbox: Sandbox,
    ):
        self.info = info
        self.handle = handle
        self._backend = backend
        self._sandbox = sandbox
        self.buffer = bytearray()
        self.condition = threading.Condition()
        self.completed = threading.Event()
        self._started_monotonic = time.monotonic()

    def append_output(self, data: bytes) -> None:
        if not data:
            return
        with self.condition:
            self.buffer.extend(data)
            self.condition.notify_all()

    def handle_exit(self, exit_code: int) -> None:
        with self.condition:
            if self.completed.is_set():
                return
            self.info.exit_code = exit_code
            self.info.state = "exited"
            self.info.finished_at = _now()
            self.info.duration_ms = round(
                (time.monotonic() - self._started_monotonic) * 1000
            )
            self.condition.notify_all()
        try:
            self._backend.cleanup_pty(self._sandbox, self.info.id)
        except Exception:
            pass
        with self.condition:
            self.completed.set()
            self.condition.notify_all()

    def set_state(self, state: str) -> None:
        with self.condition:
            if not self.completed.is_set():
                self.info.state = state
                self.condition.notify_all()

    def read_output(self, offset: int, wait_seconds: float) -> bytes:
        """Return buffered bytes from *offset*, blocking up to *wait_seconds*
        for new data when caught up. Returns ``b""`` if nothing new arrives."""
        if offset < 0:
            raise PtyServiceError("output offset cannot be negative")
        with self.condition:
            if offset > len(self.buffer):
                raise PtyServiceError("output offset is beyond available data")
            if (
                offset == len(self.buffer)
                and not self.completed.is_set()
                and wait_seconds > 0
            ):
                self.condition.wait(wait_seconds)
            data = bytes(self.buffer[offset:])
            return data

    def send_input(self, data: bytes) -> None:
        if self.completed.is_set():
            raise PtyConflictError("cannot send input to an exited PTY")
        try:
            self._backend.send_pty_input(self._sandbox, self.handle, data)
        except NotImplementedError as error:
            raise PtyUnavailableError(
                f"backend {self._sandbox.backend} cannot send PTY input"
            ) from error

    def resize(self, rows: int, cols: int) -> None:
        if self.completed.is_set():
            return
        try:
            self._backend.resize_pty(self._sandbox, self.handle, rows, cols)
        except NotImplementedError as error:
            raise PtyUnavailableError(
                f"backend {self._sandbox.backend} cannot resize PTY"
            ) from error
        with self.condition:
            self.info.rows = rows
            self.info.cols = cols
            self.condition.notify_all()

    def kill(self) -> None:
        if self.completed.is_set():
            return
        try:
            self._backend.kill_pty(self._sandbox, self.handle)
        except NotImplementedError as error:
            raise PtyUnavailableError(
                f"backend {self._sandbox.backend} cannot kill PTY"
            ) from error

    def wait(self, timeout: float | None = None) -> bool:
        return self.completed.wait(timeout)


class PtyService:
    """In-process PTY controller with reconnectable output buffers."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        self._sessions: dict[str, _PtySession] = {}
        self._lock = threading.RLock()

    def start(
        self,
        sandbox_id: str,
        *,
        rows: int = 24,
        cols: int = 80,
        command: str | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
    ) -> ManagedPty:
        if rows < 1 or cols < 1:
            raise PtyServiceError("rows and cols must be at least 1")
        if command is not None and ("\x00" in command):
            raise PtyServiceError("command cannot contain null bytes")
        sandbox = self._running_sandbox(sandbox_id)
        pty_id = f"pty_{uuid4().hex[:12]}"
        info = ManagedPty(
            id=pty_id,
            sandbox_id=sandbox.id,
            pid=0,
            rows=rows,
            cols=cols,
            command=command,
            state="running",
            started_at=_now(),
        )
        session = _PtySession(info, None, self.orchestrator.backend, sandbox)
        try:
            handle, pid = self.orchestrator.backend.start_pty(
                sandbox,
                pty_id,
                rows=rows,
                cols=cols,
                command=command,
                cwd=cwd,
                envs=envs,
                on_output=session.append_output,
                on_exit=session.handle_exit,
            )
        except NotImplementedError as error:
            raise PtyUnavailableError(
                f"backend {sandbox.backend} does not support PTY"
            ) from error
        session.handle = handle
        session.info.pid = pid
        with self._lock:
            self._sessions[pty_id] = session
        self.orchestrator._event(
            "pty_started",
            "pty",
            pty_id,
            sandbox_id=sandbox.id,
            pid=pid,
            rows=rows,
            cols=cols,
            command=command,
        )
        return info

    def list(self, sandbox_id: str) -> list[ManagedPty]:
        self.orchestrator.get_sandbox(sandbox_id)
        with self._lock:
            return [
                session.info
                for session in self._sessions.values()
                if session.info.sandbox_id == sandbox_id
            ]

    def session(self, sandbox_id: str, pty_id: str) -> _PtySession:
        """Public accessor for the transport layer (raises if not found)."""
        return self._session(sandbox_id, pty_id)

    def get(self, sandbox_id: str, pty_id: str) -> ManagedPty:
        return self._session(sandbox_id, pty_id).info

    def read_output(
        self,
        sandbox_id: str,
        pty_id: str,
        *,
        offset: int = 0,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        if wait_seconds < 0 or wait_seconds > 30:
            raise PtyServiceError("wait_seconds must be between 0 and 30")
        session = self._session(sandbox_id, pty_id)
        data = session.read_output(offset, wait_seconds)
        return {
            "pty": session.info.to_dict(),
            "data": data.decode("utf-8", errors="replace"),
            "dataBase64": data.hex(),  # raw bytes as hex for fidelity
            "next": len(session.buffer),
        }

    def send_input(
        self,
        sandbox_id: str,
        pty_id: str,
        data: bytes,
    ) -> ManagedPty:
        session = self._session(sandbox_id, pty_id)
        session.send_input(data)
        return session.info

    def resize(
        self, sandbox_id: str, pty_id: str, rows: int, cols: int
    ) -> ManagedPty:
        if rows < 1 or cols < 1:
            raise PtyServiceError("rows and cols must be at least 1")
        session = self._session(sandbox_id, pty_id)
        session.resize(rows, cols)
        return session.info

    def kill(self, sandbox_id: str, pty_id: str) -> ManagedPty:
        session = self._session(sandbox_id, pty_id)
        session.kill()
        return session.info

    def wait(
        self,
        sandbox_id: str,
        pty_id: str,
        timeout: float | None = None,
    ) -> ManagedPty:
        if timeout is not None and timeout < 0:
            raise PtyServiceError("timeout cannot be negative")
        session = self._session(sandbox_id, pty_id)
        if not session.wait(timeout):
            raise PtyConflictError(f"PTY did not exit after timeout: {pty_id}")
        return session.info

    def pause_sandbox(self, sandbox: Sandbox) -> None:
        for session in self._active_sessions(sandbox.id):
            session.set_state("paused")

    def resume_sandbox(self, sandbox: Sandbox) -> None:
        for session in self._active_sessions(sandbox.id):
            session.set_state("running")

    def terminate_sandbox(self, sandbox: Sandbox) -> None:
        sessions = self._active_sessions(sandbox.id)
        for session in sessions:
            try:
                session.kill()
            except PtyUnavailableError:
                pass
        for session in sessions:
            session.wait(2)
        with self._lock:
            for pty_id in [
                item.info.id
                for item in self._sessions.values()
                if item.info.sandbox_id == sandbox.id
            ]:
                self._sessions.pop(pty_id, None)

    def _running_sandbox(self, sandbox_id: str) -> Sandbox:
        sandbox = self.orchestrator.get_sandbox(sandbox_id)
        self.orchestrator.ensure_backend(sandbox)
        return self.orchestrator.prepare_activity(sandbox, "pty")

    def _session(self, sandbox_id: str, pty_id: str) -> _PtySession:
        self.orchestrator.get_sandbox(sandbox_id)
        with self._lock:
            session = self._sessions.get(pty_id)
        if session is None or session.info.sandbox_id != sandbox_id:
            raise PtyNotFoundError(f"pty not found: {pty_id}")
        return session

    def _active_sessions(self, sandbox_id: str) -> list[_PtySession]:
        with self._lock:
            return [
                session
                for session in self._sessions.values()
                if session.info.sandbox_id == sandbox_id
                and not session.completed.is_set()
            ]

    def _pty_exited(self, session: _PtySession) -> None:
        self.orchestrator._event(
            "pty_exited",
            "pty",
            session.info.id,
            sandbox_id=session.info.sandbox_id,
            exit_code=session.info.exit_code,
            duration_ms=session.info.duration_ms,
        )
