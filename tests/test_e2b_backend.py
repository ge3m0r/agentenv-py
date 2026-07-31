"""Unit tests for the E2B backend using a fake E2B SDK.

These tests never touch the network: they inject a stub ``Sandbox`` class into
:class:`E2BSandboxBackend` and assert that the backend maps the project's
lifecycle calls onto the correct E2B SDK calls and translates results back.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentenv.e2b_backend import E2BSandboxBackend
from agentenv.models import (
    CommandResult,
    NetworkPolicy,
    ResourceLimits,
    Sandbox,
    SandboxState,
    Snapshot,
    Template,
)


class FakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = None


class FakeCommands:
    def __init__(self, sandbox: "FakeSandbox"):
        self._sandbox = sandbox

    def run(self, *, cmd, envs=None, cwd=None, timeout=None):
        self._sandbox.calls.append(("commands.run", cmd, envs, cwd, timeout))
        return FakeCommandResult(stdout=f"ran: {cmd}", exit_code=0)


class FakeSandbox:
    """A minimal stand-in for ``e2b.Sandbox`` that records every call."""

    next_id = 0

    def __init__(self, sandbox_id: str):
        self.sandbox_id = sandbox_id
        self.commands = FakeCommands(self)
        self.calls: list[tuple[Any, ...]] = []

    # ---- classmethods invoked as ``Sandbox.method(sandbox_id=...)`` ----
    @classmethod
    def create(cls, **kwargs: Any) -> "FakeSandbox":
        cls.create_calls.append(kwargs)
        cls.next_id += 1
        sbx = cls(f"e2b-sbx-{cls.next_id}")
        cls.created.append(sbx)
        return sbx

    @classmethod
    def connect(cls, *, sandbox_id, **kwargs):
        cls.connect_calls.append(sandbox_id)
        sbx = cls(sandbox_id)
        cls.connect_instances.append(sbx)
        return sbx

    @classmethod
    def pause(cls, *, sandbox_id, keep_memory=True, **kwargs):
        cls.pause_calls.append(sandbox_id)

    @classmethod
    def kill(cls, *, sandbox_id, **kwargs):
        cls.kill_calls.append(sandbox_id)

    @classmethod
    def get_info(cls, *, sandbox_id, **kwargs):
        cls.info_calls.append(sandbox_id)

    @classmethod
    def create_snapshot(cls, *, sandbox_id, name=None, **kwargs):
        cls.snapshot_calls.append((sandbox_id, name))
        cls.next_id += 1
        snap_id = f"e2b-snap-{cls.next_id}"
        cls.snapshots_made.append(snap_id)
        return type("Info", (), {"snapshot_id": snap_id, "names": [name]})()

    @classmethod
    def delete_snapshot(cls, *, snapshot_id, **kwargs):
        cls.snapshot_deletes.append(snapshot_id)

    @classmethod
    def update_network(cls, *, sandbox_id, network, **kwargs):
        cls.network_updates.append((sandbox_id, network))


def _fresh_fake():
    """Rebuild the fake class with empty call logs for each test."""
    return type("FakeSandbox", (FakeSandbox,), {
        "create_calls": [],
        "connect_calls": [],
        "connect_instances": [],
        "pause_calls": [],
        "kill_calls": [],
        "info_calls": [],
        "snapshot_calls": [],
        "snapshots_made": [],
        "snapshot_deletes": [],
        "network_updates": [],
        "created": [],
        "next_id": 0,
    })


def _sandbox(**overrides) -> Sandbox:
    defaults: dict[str, Any] = dict(
        id="sbx_test",
        template_id="tpl_test",
        workspace_path="/tmp/sbx_test",
        state=SandboxState.CREATING,
        env={"NAME": "AgentENV"},
        workdir=".",
        runtime_id=None,
        timeout_seconds=600,
        metadata={"task": "unit"},
        network=NetworkPolicy(),
        resources=ResourceLimits(),
    )
    defaults.update(overrides)
    return Sandbox(**defaults)


class E2BBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _fresh_fake()
        self.backend = E2BSandboxBackend(
            sandbox_class=self.fake, api_key="test-key"
        )

    def test_requires_api_key(self) -> None:
        with self.assertRaises(Exception):
            E2BSandboxBackend(sandbox_class=_fresh_fake(), api_key="")

    def test_create_maps_template_and_lifecycle(self) -> None:
        template = Template(
            id="tpl", name="demo", source="base", rootfs_path="/tmp/tpl"
        )
        sandbox = _sandbox(timeout_action="pause", auto_resume=True)
        self.backend.create(template, sandbox)

        self.assertEqual("e2b-sbx-1", sandbox.runtime_id)
        self.assertEqual("base", sandbox.image_ref)
        kwargs = self.fake.create_calls[0]
        self.assertEqual("base", kwargs["template"])
        self.assertEqual(600, kwargs["timeout"])
        self.assertEqual({"NAME": "AgentENV"}, kwargs["envs"])
        self.assertEqual({"task": "unit"}, kwargs["metadata"])
        self.assertTrue(kwargs["allow_internet_access"])
        self.assertEqual(
            {"on_timeout": "pause", "auto_resume": True}, kwargs["lifecycle"]
        )

    def test_scratch_source_uses_default_template(self) -> None:
        template = Template(id="tpl", name="d", source="scratch", rootfs_path="/x")
        self.backend.create(template, _sandbox())
        self.assertIsNone(self.fake.create_calls[0]["template"])

    def test_execute_runs_via_connect_and_commands(self) -> None:
        sandbox = _sandbox(runtime_id="e2b-live")
        result = self.backend.execute(sandbox, "echo hi", timeout=5)

        self.assertIsInstance(result, CommandResult)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("ran: echo hi", result.stdout)
        self.assertEqual(["e2b-live"], self.fake.connect_calls)
        run = self.fake.connect_instances[-1].calls[0]
        self.assertEqual("commands.run", run[0])
        self.assertEqual("echo hi", run[1])
        self.assertEqual(5, run[4])

    def test_pause_resume_kill_use_sandbox_id(self) -> None:
        sandbox = _sandbox(runtime_id="e2b-live")
        self.backend.pause(sandbox)
        self.backend.resume(sandbox)
        self.backend.destroy(sandbox)
        self.assertEqual(["e2b-live"], self.fake.pause_calls)
        self.assertEqual(["e2b-live"], self.fake.connect_calls)
        self.assertEqual(["e2b-live"], self.fake.kill_calls)

    def test_runtime_alive_uses_get_info(self) -> None:
        sandbox = _sandbox(runtime_id="e2b-live")
        self.assertTrue(self.backend.runtime_alive(sandbox))
        self.assertEqual(["e2b-live"], self.fake.info_calls)

    def test_update_network_maps_policy(self) -> None:
        sandbox = _sandbox(runtime_id="e2b-live")
        policy = NetworkPolicy(
            allow_internet_access=False, deny_out=["0.0.0.0/0"]
        )
        self.backend.update_network(sandbox, policy)
        _, network = self.fake.network_updates[0]
        self.assertFalse(network["allow_internet_access"])
        self.assertEqual(["0.0.0.0/0"], network["deny_out"])

    def test_update_resources_is_noop(self) -> None:
        sandbox = _sandbox(runtime_id="e2b-live")
        # Should not raise and should not call the SDK.
        self.backend.update_resources(sandbox, ResourceLimits())
        self.assertEqual([], self.fake.info_calls)

    def test_snapshot_capture_writes_sidecar_and_restore_boots_from_it(self) -> None:
        self.maxDiff = None
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        dest = Path(self.tempdir.name) / "rootfs"

        sandbox = _sandbox(runtime_id="e2b-live")
        self.backend.capture(sandbox, dest)

        sidecar = dest / "e2b_snapshot.json"
        self.assertTrue(sidecar.exists())
        e2b_snap_id = json.loads(sidecar.read_text())["e2b_snapshot_id"]
        self.assertEqual("e2b-snap-1", e2b_snap_id)
        self.assertEqual([("e2b-live", "sbx_test")], self.fake.snapshot_calls)

        snapshot = Snapshot(
            id="snp_test",
            sandbox_id=sandbox.id,
            rootfs_path=str(dest),
            backend="e2b",
        )
        restored = _sandbox()
        self.backend.restore(snapshot, restored)
        self.assertEqual(e2b_snap_id, self.fake.create_calls[0]["template"])
        self.assertEqual(e2b_snap_id, restored.image_ref)
        self.assertEqual("e2b-sbx-2", restored.runtime_id)

    def test_delete_snapshot_calls_cloud_delete(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        dest = Path(self.tempdir.name) / "rootfs"
        self.backend.capture(_sandbox(runtime_id="e2b-live"), dest)
        snapshot = Snapshot(
            id="snp", sandbox_id="sbx", rootfs_path=str(dest), backend="e2b"
        )
        self.backend.delete_snapshot(snapshot)
        self.assertEqual(["e2b-snap-1"], self.fake.snapshot_deletes)


if __name__ == "__main__":
    unittest.main()
