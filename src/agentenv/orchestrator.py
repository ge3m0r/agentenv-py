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
    NetworkPolicy,
    ResourceLimits,
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

    def _ensure_backend(self, sandbox: Sandbox) -> None:
        if sandbox.backend != self.backend.name:
            raise ConflictError(
                f"sandbox uses backend {sandbox.backend}, "
                f"but this process uses {self.backend.name}"
            )

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
            try:
                template = self.backend.prepare_template(template)
            except Exception:
                shutil.rmtree(rootfs.parent, ignore_errors=True)
                raise
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
        resources: ResourceLimits | dict[str, Any] | None = None,
        network: NetworkPolicy | dict[str, Any] | None = None,
        timeout_action: str | None = None,
        auto_resume: bool | None = None,
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
                if snapshot.backend != self.backend.name:
                    raise ConflictError(
                        f"snapshot uses backend {snapshot.backend}, "
                        f"but this process uses {self.backend.name}"
                    )
                base_resources = snapshot.resources
                base_network = snapshot.network
                image_ref = snapshot.image_ref
                base_timeout_action = snapshot.timeout_action
                base_auto_resume = snapshot.auto_resume
            else:
                template = self.store.get_template(template_id or "")
                if not template:
                    raise NotFoundError(f"template not found: {template_id}")
                base_env, workdir, source_id = (
                    template.env,
                    template.workdir,
                    template.id,
                )
                base_resources = ResourceLimits()
                base_network = NetworkPolicy()
                image_ref = template.image_ref
                base_timeout_action = "kill"
                base_auto_resume = False
            resolved_timeout_action = timeout_action or base_timeout_action
            resolved_auto_resume = (
                base_auto_resume if auto_resume is None else auto_resume
            )
            if resolved_timeout_action not in ("kill", "pause"):
                raise AgentEnvError("timeout_action must be kill or pause")
            if resolved_auto_resume and resolved_timeout_action != "pause":
                raise AgentEnvError("auto_resume requires timeout_action=pause")
            resolved_resources = (
                resources
                if isinstance(resources, ResourceLimits)
                else ResourceLimits.from_dict(resources)
                if resources is not None
                else base_resources
            )
            resolved_resources.validate()
            resolved_network = (
                network
                if isinstance(network, NetworkPolicy)
                else NetworkPolicy.from_dict(network)
                if network is not None
                else base_network
            )
            resolved_network.validate()
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
                backend=self.backend.name,
                image_ref=image_ref,
                resources=resolved_resources,
                network=resolved_network,
                timeout_action=resolved_timeout_action,
                auto_resume=resolved_auto_resume,
                timeout_seconds=timeout_seconds,
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
        self._ensure_backend(sandbox)
        if sandbox.state == SandboxState.PAUSED and sandbox.auto_resume:
            sandbox = self.resume(sandbox.id)
            if sandbox.timeout_seconds:
                sandbox = self.update_timeout(
                    sandbox.id, sandbox.timeout_seconds
                )
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
            self._ensure_backend(sandbox)
            if sandbox.state == SandboxState.PAUSED:
                return sandbox
            if sandbox.state != SandboxState.RUNNING:
                raise AgentEnvError(f"cannot pause sandbox in state {sandbox.state.value}")
            with self._transition(
                sandbox, SandboxState.PAUSING, SandboxState.PAUSED
            ):
                self.backend.pause(sandbox)
            self._event("sandbox_paused", "sandbox", sandbox.id)
            return sandbox

    def resume(self, sandbox_id: str) -> Sandbox:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            self._ensure_backend(sandbox)
            if sandbox.state == SandboxState.RUNNING:
                return sandbox
            if sandbox.state != SandboxState.PAUSED:
                raise AgentEnvError(f"cannot resume sandbox in state {sandbox.state.value}")
            with self._transition(
                sandbox, SandboxState.RESUMING, SandboxState.RUNNING
            ):
                self.backend.resume(sandbox)
            self._event("sandbox_resumed", "sandbox", sandbox.id)
            return sandbox

    def snapshot(self, sandbox_id: str) -> Snapshot:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            self._ensure_backend(sandbox)
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
                    backend=sandbox.backend,
                    image_ref=sandbox.image_ref,
                    resources=sandbox.resources,
                    network=sandbox.network,
                    timeout_action=sandbox.timeout_action,
                    auto_resume=sandbox.auto_resume,
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
            self._ensure_backend(source)
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
                    backend=source.backend,
                    image_ref=source.image_ref,
                    resources=source.resources,
                    network=source.network,
                    timeout_action=source.timeout_action,
                    auto_resume=source.auto_resume,
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
            self._ensure_backend(sandbox)
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
            sandbox.timeout_seconds = timeout_seconds
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            self._event(
                "sandbox_timeout_updated",
                "sandbox",
                sandbox.id,
                timeout_at=sandbox.timeout_at,
            )
            return sandbox

    def update_network(
        self, sandbox_id: str, policy: NetworkPolicy | dict[str, Any]
    ) -> Sandbox:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            self._ensure_backend(sandbox)
            if sandbox.state != SandboxState.RUNNING:
                raise ConflictError("network can only be updated while running")
            resolved = (
                policy
                if isinstance(policy, NetworkPolicy)
                else NetworkPolicy.from_dict(policy)
            )
            resolved.validate()
            self.backend.update_network(sandbox, resolved)
            sandbox.network = resolved
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            self._event(
                "sandbox_network_updated",
                "sandbox",
                sandbox.id,
                network=resolved.__dict__,
            )
            return sandbox

    def update_resources(
        self, sandbox_id: str, resources: ResourceLimits | dict[str, Any]
    ) -> Sandbox:
        with self._lock:
            sandbox = self.get_sandbox(sandbox_id)
            self._ensure_backend(sandbox)
            if sandbox.state != SandboxState.RUNNING:
                raise ConflictError("resources can only be updated while running")
            resolved = (
                resources
                if isinstance(resources, ResourceLimits)
                else ResourceLimits.from_dict(resources)
            )
            resolved.validate()
            self.backend.update_resources(sandbox, resolved)
            sandbox.resources = resolved
            sandbox.updated_at = _now()
            self.store.put_sandbox(sandbox)
            self._event(
                "sandbox_resources_updated",
                "sandbox",
                sandbox.id,
                resources=resolved.__dict__,
            )
            return sandbox

    def create_cold_sandbox(
        self,
        image: str,
        *,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
        resources: ResourceLimits | dict[str, Any] | None = None,
        network: NetworkPolicy | dict[str, Any] | None = None,
        timeout_action: str = "kill",
        auto_resume: bool = False,
    ) -> Sandbox:
        if self.backend.name != "docker":
            raise ConflictError("cold OCI sandbox creation requires the Docker backend")
        template = self.create_template(
            name=f"cold-{uuid4().hex[:10]}",
            source=image,
        )
        return self.create_sandbox(
            template.id,
            timeout_seconds=timeout_seconds,
            env=env,
            metadata=metadata,
            resources=resources,
            network=network,
            timeout_action=timeout_action,
            auto_resume=auto_resume,
        )

    def evict_expired(self) -> list[str]:
        now = datetime.now(timezone.utc)
        evicted = []
        for sandbox in self.list_sandboxes():
            if (
                sandbox.backend == self.backend.name
                and sandbox.timeout_at
                and datetime.fromisoformat(sandbox.timeout_at) <= now
            ):
                if sandbox.timeout_action == "pause":
                    self.pause(sandbox.id)
                    paused = self.get_sandbox(sandbox.id)
                    paused.timeout_at = None
                    paused.updated_at = _now()
                    self.store.put_sandbox(paused)
                    self._event(
                        "sandbox_auto_paused", "sandbox", sandbox.id
                    )
                else:
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

        The selected backend decides whether the durable runtime still exists.
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
            if sandbox.backend != self.backend.name:
                continue
            runtime_alive = self.backend.runtime_alive(sandbox)
            if previous == SandboxState.KILLING or not runtime_alive:
                if runtime_alive:
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
