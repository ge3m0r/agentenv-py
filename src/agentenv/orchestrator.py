from __future__ import annotations

import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4

from .backend import LocalProcessBackend, SandboxBackend
from .models import (
    CommandResult,
    LifecycleEvent,
    Sandbox,
    SandboxState,
    Snapshot,
    Template,
)
from .store import JsonMetadataStore


class AgentEnvError(RuntimeError):
    pass


class NotFoundError(AgentEnvError):
    pass


class ConflictError(AgentEnvError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Orchestrator:
    """Owns lifecycle state transitions; the backend owns runtime mechanics."""

    def __init__(
        self,
        data_dir: str | Path = ".agentenv",
        backend: SandboxBackend | None = None,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.templates_dir = self.data_dir / "templates"
        self.sandboxes_dir = self.data_dir / "sandboxes"
        self.snapshots_dir = self.data_dir / "snapshots"
        for directory in (
            self.templates_dir,
            self.sandboxes_dir,
            self.snapshots_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.store = JsonMetadataStore(self.data_dir)
        self.backend = backend or LocalProcessBackend()
        self._lock = RLock()
        self.recover_interrupted_operations()

    def _event(
        self,
        event_type: str,
        resource_type: str,
        resource_id: str,
        **details: Any,
    ) -> LifecycleEvent:
        event = LifecycleEvent(
            id=_id("evt"),
            type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            occurred_at=_now(),
            details=details,
        )
        self.store.put_event(event)
        return event

    @contextmanager
    def _transition(
        self,
        sandbox: Sandbox,
        transient: SandboxState,
        success: SandboxState,
    ) -> Iterator[None]:
        previous = sandbox.state
        sandbox.state = transient
        sandbox.updated_at = _now()
        self.store.put_sandbox(sandbox)
        try:
            yield
        except Exception as error:
            sandbox.state = previous
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            self._event(
                "operation_failed",
                "sandbox",
                sandbox.id,
                operation=transient.value,
                error=str(error),
                rolled_back_to=previous.value,
            )
            raise
        sandbox.state = success
        sandbox.updated_at = _now()
        self.store.put_sandbox(sandbox)

    def create_template(
        self,
        name: str,
        source: str = "scratch",
        base_dir: str | Path | None = None,
        env: dict[str, str] | None = None,
        workdir: str = ".",
    ) -> Template:
        with self._lock:
            if not name.strip():
                raise AgentEnvError("template name cannot be empty")
            if self.store.get_template(name):
                raise ConflictError(f"template already exists: {name}")
            template_id = _id("tpl")
            rootfs = self.templates_dir / template_id / "rootfs"
            if base_dir:
                base = Path(base_dir).resolve()
                if not base.is_dir():
                    raise AgentEnvError(f"base directory does not exist: {base}")
                shutil.copytree(base, rootfs)
            else:
                rootfs.mkdir(parents=True)
            template = Template(
                id=template_id,
                name=name,
                source=source,
                rootfs_path=str(rootfs),
                env=env or {},
                workdir=workdir,
                created_at=_now(),
            )
            self.store.put_template(template)
            self._event(
                "template_created",
                "template",
                template.id,
                name=template.name,
                source=template.source,
            )
            return template

    def list_templates(self) -> list[Template]:
        return self.store.list_templates()

    def delete_template(self, template_id: str) -> None:
        with self._lock:
            template = self.store.get_template(template_id)
            if not template:
                raise NotFoundError(f"template not found: {template_id}")
            in_use = [
                sandbox.id
                for sandbox in self.list_sandboxes()
                if sandbox.template_id == template.id
            ]
            if in_use:
                raise ConflictError(
                    f"template is used by {len(in_use)} sandbox(es): {', '.join(in_use)}"
                )
            root = Path(template.rootfs_path).parent
            if root.exists():
                shutil.rmtree(root)
            self.store.delete_template(template.id)
            self._event(
                "template_deleted", "template", template.id, name=template.name
            )

    def create_sandbox(
        self,
        template_id: str | None = None,
        *,
        snapshot_id: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Sandbox:
        with self._lock:
            if timeout_seconds is not None and timeout_seconds <= 0:
                raise AgentEnvError("timeout_seconds must be greater than zero")
            if bool(template_id) == bool(snapshot_id):
                raise AgentEnvError("provide exactly one of template_id or snapshot_id")
            snapshot = None
            if snapshot_id:
                snapshot = self.store.get_snapshot(snapshot_id)
                if not snapshot:
                    raise NotFoundError(f"snapshot not found: {snapshot_id}")
                base_env, workdir, source_id = (
                    snapshot.env,
                    snapshot.workdir,
                    f"snapshot:{snapshot.id}",
                )
            else:
                template = self.store.get_template(template_id or "")
                if not template:
                    raise NotFoundError(f"template not found: {template_id}")
                base_env, workdir, source_id = (
                    template.env,
                    template.workdir,
                    template.id,
                )
            now = _now()
            sandbox_id = _id("sbx")
            timeout_at = None
            if timeout_seconds is not None:
                timeout_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
                ).isoformat()
            sandbox = Sandbox(
                id=sandbox_id,
                template_id=source_id,
                workspace_path=str(self.sandboxes_dir / sandbox_id / "rootfs"),
                state=SandboxState.CREATING,
                env={**base_env, **(env or {})},
                workdir=workdir,
                timeout_at=timeout_at,
                metadata=metadata or {},
                created_at=now,
                updated_at=now,
            )
            self.store.put_sandbox(sandbox)
            try:
                if snapshot:
                    self.backend.restore(snapshot, sandbox)
                else:
                    self.backend.create(template, sandbox)
            except Exception as error:
                self.store.delete_sandbox(sandbox.id)
                self._event(
                    "sandbox_create_failed",
                    "sandbox",
                    sandbox.id,
                    error=str(error),
                    source=source_id,
                )
                raise
            sandbox.state = SandboxState.RUNNING
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            self._event(
                "sandbox_created",
                "sandbox",
                sandbox.id,
                source=source_id,
                timeout_at=timeout_at,
            )
            return sandbox

    def list_sandboxes(self) -> list[Sandbox]:
        return self.store.list_sandboxes()

    def get_sandbox(self, sandbox_id: str) -> Sandbox:
        sandbox = self.store.get_sandbox(sandbox_id)
        if not sandbox:
            raise NotFoundError(f"sandbox not found: {sandbox_id}")
        return sandbox

    def execute(
        self, sandbox_id: str, command: str, timeout: float | None = None
    ) -> CommandResult:
        sandbox = self.get_sandbox(sandbox_id)
        if sandbox.state != SandboxState.RUNNING:
            raise AgentEnvError(
                f"sandbox {sandbox_id} is {sandbox.state.value}; execution requires running"
            )
        result = self.backend.execute(sandbox, command, timeout)
        self._event(
            "command_executed",
            "sandbox",
            sandbox.id,
            command=command,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )
        return result

    def pause(self, sandbox_id: str) -> Sandbox:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            if sandbox.state == SandboxState.PAUSED:
                return sandbox
            if sandbox.state != SandboxState.RUNNING:
                raise AgentEnvError(f"cannot pause sandbox in state {sandbox.state.value}")
            with self._transition(
                sandbox, SandboxState.PAUSING, SandboxState.PAUSED
            ):
                pass
            self._event("sandbox_paused", "sandbox", sandbox.id)
            return sandbox

    def resume(self, sandbox_id: str) -> Sandbox:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            if sandbox.state == SandboxState.RUNNING:
                return sandbox
            if sandbox.state != SandboxState.PAUSED:
                raise AgentEnvError(f"cannot resume sandbox in state {sandbox.state.value}")
            with self._transition(
                sandbox, SandboxState.RESUMING, SandboxState.RUNNING
            ):
                if not Path(sandbox.workspace_path).exists():
                    raise AgentEnvError("paused workspace is missing")
            self._event("sandbox_resumed", "sandbox", sandbox.id)
            return sandbox

    def snapshot(self, sandbox_id: str) -> Snapshot:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            if sandbox.state != SandboxState.RUNNING:
                raise AgentEnvError("only a running sandbox can be snapshotted")
            snapshot_id = _id("snp")
            rootfs = self.snapshots_dir / snapshot_id / "rootfs"
            with self._transition(
                sandbox, SandboxState.SNAPSHOTTING, SandboxState.RUNNING
            ):
                self.backend.capture(sandbox, rootfs)
                snapshot = Snapshot(
                    id=snapshot_id,
                    sandbox_id=sandbox.id,
                    rootfs_path=str(rootfs),
                    env=dict(sandbox.env),
                    workdir=sandbox.workdir,
                    created_at=_now(),
                )
                self.store.put_snapshot(snapshot)
            self._event(
                "snapshot_created",
                "snapshot",
                snapshot.id,
                sandbox_id=sandbox.id,
            )
            return snapshot

    def list_snapshots(self) -> list[Snapshot]:
        return self.store.list_snapshots()

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        snapshot = self.store.get_snapshot(snapshot_id)
        if not snapshot:
            raise NotFoundError(f"snapshot not found: {snapshot_id}")
        return snapshot

    def delete_snapshot(self, snapshot_id: str) -> None:
        with self._lock:
            snapshot = self.get_snapshot(snapshot_id)
            source_id = f"snapshot:{snapshot.id}"
            in_use = [
                sandbox.id
                for sandbox in self.list_sandboxes()
                if sandbox.template_id == source_id
            ]
            if in_use:
                raise ConflictError(
                    f"snapshot is used by {len(in_use)} sandbox(es): {', '.join(in_use)}"
                )
            root = Path(snapshot.rootfs_path).parent
            if root.exists():
                shutil.rmtree(root)
            self.store.delete_snapshot(snapshot.id)
            self._event(
                "snapshot_deleted",
                "snapshot",
                snapshot.id,
                sandbox_id=snapshot.sandbox_id,
            )

    def fork(self, sandbox_id: str, count: int = 1) -> list[Sandbox]:
        if count < 1:
            raise AgentEnvError("count must be at least 1")
        with self._lock:
            source = self.get_sandbox(sandbox_id)
            if source.state != SandboxState.RUNNING:
                raise AgentEnvError("only a running sandbox can be forked")
            children: list[Sandbox] = []
            with self._transition(
                source, SandboxState.FORKING, SandboxState.RUNNING
            ):
                snapshot_id = _id("snp")
                rootfs = self.snapshots_dir / snapshot_id / "rootfs"
                self.backend.capture(source, rootfs)
                snapshot = Snapshot(
                    id=snapshot_id,
                    sandbox_id=source.id,
                    rootfs_path=str(rootfs),
                    env=dict(source.env),
                    workdir=source.workdir,
                    created_at=_now(),
                )
                self.store.put_snapshot(snapshot)
                self._event(
                    "snapshot_created",
                    "snapshot",
                    snapshot.id,
                    sandbox_id=source.id,
                    purpose="fork",
                )
                for _ in range(count):
                    children.append(self.create_sandbox(snapshot_id=snapshot.id))
            self._event(
                "sandbox_forked",
                "sandbox",
                source.id,
                snapshot_id=snapshot.id,
                child_ids=[child.id for child in children],
            )
            return children

    def delete(self, sandbox_id: str) -> None:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            previous = sandbox.state
            sandbox.state = SandboxState.KILLING
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            try:
                self.backend.destroy(sandbox)
            except Exception:
                sandbox.state = previous
                self.store.put_sandbox(sandbox)
                raise
            self.store.delete_sandbox(sandbox.id)
            self._event(
                "sandbox_deleted",
                "sandbox",
                sandbox.id,
                previous_state=previous.value,
            )

    def update_timeout(
        self, sandbox_id: str, timeout_seconds: int | None
    ) -> Sandbox:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            if timeout_seconds is not None and timeout_seconds <= 0:
                raise AgentEnvError("timeout_seconds must be greater than zero")
            sandbox.timeout_at = (
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=timeout_seconds)
                ).isoformat()
                if timeout_seconds is not None
                else None
            )
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            self._event(
                "sandbox_timeout_updated",
                "sandbox",
                sandbox.id,
                timeout_at=sandbox.timeout_at,
            )
            return sandbox

    def evict_expired(self) -> list[str]:
        now = datetime.now(timezone.utc)
        evicted = []
        for sandbox in self.list_sandboxes():
            if sandbox.timeout_at and datetime.fromisoformat(sandbox.timeout_at) <= now:
                self.delete(sandbox.id)
                evicted.append(sandbox.id)
        return evicted

    def list_events(self, limit: int | None = 100) -> list[LifecycleEvent]:
        if limit is not None and limit < 1:
            raise AgentEnvError("event limit must be greater than zero")
        return self.store.list_events(limit)

    def status(self) -> dict[str, Any]:
        sandboxes = self.list_sandboxes()
        state_counts = {state.value: 0 for state in SandboxState}
        for sandbox in sandboxes:
            state_counts[sandbox.state.value] += 1
        return {
            "templates": len(self.list_templates()),
            "sandboxes": len(sandboxes),
            "snapshots": len(self.list_snapshots()),
            "sandbox_states": state_counts,
            "backend": type(self.backend).__name__,
            "data_dir": str(self.data_dir),
        }

    def recover_interrupted_operations(self) -> list[str]:
        """
        Reconcile durable transitional states left behind by a process crash.

        The local backend has no resident VM process, so an existing workspace
        is enough to return an interrupted operation to RUNNING.
        """
        recovered: list[str] = []
        transitional = {
            SandboxState.CREATING,
            SandboxState.SNAPSHOTTING,
            SandboxState.FORKING,
            SandboxState.PAUSING,
            SandboxState.RESUMING,
            SandboxState.KILLING,
        }
        for sandbox in self.store.list_sandboxes():
            if sandbox.state not in transitional:
                continue
            previous = sandbox.state
            workspace_exists = Path(sandbox.workspace_path).exists()
            if previous == SandboxState.KILLING or not workspace_exists:
                if workspace_exists:
                    self.backend.destroy(sandbox)
                self.store.delete_sandbox(sandbox.id)
                action = "removed"
            else:
                sandbox.state = SandboxState.RUNNING
                sandbox.updated_at = _now()
                self.store.put_sandbox(sandbox)
                action = "restored_to_running"
            self._event(
                "sandbox_recovered",
                "sandbox",
                sandbox.id,
                interrupted_state=previous.value,
                action=action,
            )
            recovered.append(sandbox.id)
        return recovered
