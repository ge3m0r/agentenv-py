from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from ipaddress import ip_address, ip_network
import re
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
class ResourceLimits:
    cpu_count: float = 1.0
    memory_mb: int = 512
    disk_size_mb: int = 0
    pids_limit: int = 256

    def validate(self) -> None:
        if self.cpu_count <= 0:
            raise ValueError("cpu_count must be greater than zero")
        if self.memory_mb < 128:
            raise ValueError("memory_mb must be at least 128")
        if self.disk_size_mb < 0:
            raise ValueError("disk_size_mb cannot be negative")
        if self.pids_limit < 1:
            raise ValueError("pids_limit must be greater than zero")

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ResourceLimits":
        value = value or {}
        result = cls(
            cpu_count=value.get("cpu_count", value.get("cpuCount", 1.0)),
            memory_mb=value.get("memory_mb", value.get("memoryMB", 512)),
            disk_size_mb=value.get(
                "disk_size_mb", value.get("diskSizeMB", 0)
            ),
            pids_limit=value.get("pids_limit", value.get("pidsLimit", 256)),
        )
        result.validate()
        return result


@dataclass
class NetworkPolicy:
    allow_internet_access: bool = True
    allow_out: list[str] = field(default_factory=list)
    deny_out: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not isinstance(self.allow_internet_access, bool):
            raise ValueError("allow_internet_access must be a boolean")
        for destination in self.allow_out:
            if not _valid_network_destination(destination, allow_domain=True):
                raise ValueError(f"invalid allow_out destination: {destination}")
        for destination in self.deny_out:
            if not _valid_network_destination(destination, allow_domain=False):
                raise ValueError(f"invalid deny_out destination: {destination}")

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "NetworkPolicy":
        value = value or {}
        result = cls(
            allow_internet_access=value.get(
                "allow_internet_access",
                value.get("allowInternetAccess", True),
            ),
            allow_out=list(value.get("allow_out", value.get("allowOut", [])) or []),
            deny_out=list(value.get("deny_out", value.get("denyOut", [])) or []),
        )
        result.validate()
        return result


_DOMAIN = re.compile(
    r"^(?:\*\.)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,63}$"
)


def _valid_network_destination(value: str, allow_domain: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        if "/" in value:
            ip_network(value, strict=False)
        else:
            ip_address(value)
        return True
    except ValueError:
        return allow_domain and bool(_DOMAIN.fullmatch(value))


@dataclass
class Template:
    id: str
    name: str
    source: str
    rootfs_path: str
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "."
    image_ref: str | None = None
    image_digest: str | None = None
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
    backend: str = "local"
    runtime_id: str | None = None
    image_ref: str | None = None
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    timeout_action: str = "kill"
    auto_resume: bool = False
    timeout_seconds: int | None = None
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
        value["resources"] = ResourceLimits.from_dict(value.get("resources"))
        value["network"] = NetworkPolicy.from_dict(value.get("network"))
        return cls(**value)


@dataclass
class Snapshot:
    id: str
    sandbox_id: str
    rootfs_path: str
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "."
    backend: str = "local"
    image_ref: str | None = None
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    timeout_action: str = "kill"
    auto_resume: bool = False
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Snapshot":
        value = dict(value)
        value["resources"] = ResourceLimits.from_dict(value.get("resources"))
        value["network"] = NetworkPolicy.from_dict(value.get("network"))
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
