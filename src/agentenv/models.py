from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SandboxState(str, Enum):
    CREATING = "creating"
    RUNNING = "running"
    SNAPSHOTTING = "snapshotting"
    FORKING = "forking"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    KILLING = "killing"


@dataclass
class Template:
    id: str
    name: str
    source: str
    rootfs_path: str
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "."
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Template":
        return cls(**value)


@dataclass
class Sandbox:
    id: str
    template_id: str
    workspace_path: str
    state: SandboxState
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "."
    timeout_at: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Sandbox":
        value = dict(value)
        value["state"] = SandboxState(value["state"])
        return cls(**value)


@dataclass
class Snapshot:
    id: str
    sandbox_id: str
    rootfs_path: str
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "."
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Snapshot":
        return cls(**value)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    executed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifecycleEvent:
    id: str
    type: str
    resource_type: str
    resource_id: str
    occurred_at: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LifecycleEvent":
        return cls(**value)
