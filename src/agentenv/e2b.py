from __future__ import annotations

from typing import Any

from .models import NetworkPolicy, ResourceLimits, Sandbox


def resources_from_request(body: dict[str, Any]) -> ResourceLimits | None:
    value = body.get("resources")
    if value is None:
        keys = ("cpuCount", "memoryMB", "diskSizeMB", "pidsLimit")
        value = {key: body[key] for key in keys if key in body}
    return ResourceLimits.from_dict(value) if value else None


def network_from_request(body: dict[str, Any]) -> NetworkPolicy | None:
    value = dict(body.get("network") or {})
    for key in ("allowOut", "denyOut", "allow_out", "deny_out"):
        if key in body:
            value[key] = body[key]
    if "allow_internet_access" in body:
        value["allow_internet_access"] = body["allow_internet_access"]
    if "allowInternetAccess" in body:
        value["allowInternetAccess"] = body["allowInternetAccess"]
    return NetworkPolicy.from_dict(value) if value else None


def lifecycle_from_request(body: dict[str, Any]) -> tuple[str, bool]:
    lifecycle = body.get("lifecycle") or {}
    on_timeout = lifecycle.get(
        "onTimeout", lifecycle.get("on_timeout", "kill")
    )
    if body.get("autoPause"):
        on_timeout = "pause"
    auto_resume = lifecycle.get(
        "autoResume", lifecycle.get("auto_resume", False)
    )
    return on_timeout, bool(auto_resume)


def sandbox_to_e2b(sandbox: Sandbox) -> dict[str, Any]:
    state = "paused" if sandbox.state.value == "paused" else "running"
    return {
        "sandboxID": sandbox.id,
        "clientID": sandbox.id,
        "templateID": sandbox.template_id,
        "startedAt": sandbox.created_at,
        "endAt": sandbox.timeout_at,
        "state": state,
        "cpuCount": sandbox.resources.cpu_count,
        "memoryMB": sandbox.resources.memory_mb,
        "diskSizeMB": sandbox.resources.disk_size_mb,
        "envdVersion": "agentenv-py",
        "envdAccessToken": None,
        "trafficAccessToken": None,
        "domain": None,
        "alias": None,
        "metadata": sandbox.metadata,
        "allowInternetAccess": sandbox.network.allow_internet_access,
        "network": {
            "allowOut": sandbox.network.allow_out,
            "denyOut": sandbox.network.deny_out,
        },
        "backend": sandbox.backend,
        "imageRef": sandbox.image_ref,
        "lifecycle": {
            "onTimeout": sandbox.timeout_action,
            "autoResume": sandbox.auto_resume,
        },
    }
