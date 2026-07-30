from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentenv.models import NetworkPolicy, SandboxState
from agentenv.orchestrator import AgentEnvError, ConflictError, Orchestrator


class CoreFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.orchestrator = Orchestrator(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_main_flow(self) -> None:
        template = self.orchestrator.create_template(
            "python", env={"GREETING": "hello"}
        )
        sandbox = self.orchestrator.create_sandbox(template.name)
        result = self.orchestrator.execute(
            sandbox.id,
            "printf '%s world' \"$GREETING\" > message.txt && cat message.txt",
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual("hello world", result.stdout)

        paused = self.orchestrator.pause(sandbox.id)
        self.assertEqual(SandboxState.PAUSED, paused.state)
        with self.assertRaises(AgentEnvError):
            self.orchestrator.execute(sandbox.id, "true")

        resumed = self.orchestrator.resume(sandbox.id)
        self.assertEqual(SandboxState.RUNNING, resumed.state)
        snapshot = self.orchestrator.snapshot(sandbox.id)
        restored = self.orchestrator.create_sandbox(snapshot_id=snapshot.id)
        result = self.orchestrator.execute(restored.id, "cat message.txt")
        self.assertEqual("hello world", result.stdout)

        children = self.orchestrator.fork(sandbox.id, count=2)
        self.assertEqual(2, len(children))
        self.assertTrue(
            all(Path(child.workspace_path, "message.txt").exists() for child in children)
        )

        self.orchestrator.delete(sandbox.id)
        with self.assertRaises(AgentEnvError):
            self.orchestrator.get_sandbox(sandbox.id)

    def test_state_survives_restart(self) -> None:
        template = self.orchestrator.create_template("persisted")
        sandbox = self.orchestrator.create_sandbox(template.id)
        reloaded = Orchestrator(self.temporary.name)
        self.assertEqual(sandbox.id, reloaded.get_sandbox(sandbox.id).id)

    def test_timeout_events_and_status(self) -> None:
        template = self.orchestrator.create_template("ttl")
        sandbox = self.orchestrator.create_sandbox(template.id, timeout_seconds=60)
        sandbox.timeout_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        self.orchestrator.store.put_sandbox(sandbox)

        self.assertEqual([sandbox.id], self.orchestrator.evict_expired())
        status = self.orchestrator.status()
        self.assertEqual(1, status["templates"])
        self.assertEqual(0, status["sandboxes"])
        event_types = {event.type for event in self.orchestrator.list_events()}
        self.assertIn("sandbox_created", event_types)
        self.assertIn("sandbox_deleted", event_types)

    def test_delete_guards_and_cleanup(self) -> None:
        template = self.orchestrator.create_template("guarded")
        sandbox = self.orchestrator.create_sandbox(template.id)
        with self.assertRaises(ConflictError):
            self.orchestrator.delete_template(template.id)

        snapshot = self.orchestrator.snapshot(sandbox.id)
        restored = self.orchestrator.create_sandbox(snapshot_id=snapshot.id)
        with self.assertRaises(ConflictError):
            self.orchestrator.delete_snapshot(snapshot.id)

        self.orchestrator.delete(restored.id)
        self.orchestrator.delete_snapshot(snapshot.id)
        self.orchestrator.delete(sandbox.id)
        self.orchestrator.delete_template(template.id)
        self.assertFalse(Path(template.rootfs_path).parent.exists())
        self.assertFalse(Path(snapshot.rootfs_path).parent.exists())

    def test_command_timeout_returns_standard_exit_code(self) -> None:
        template = self.orchestrator.create_template("timeout-command")
        sandbox = self.orchestrator.create_sandbox(template.id)
        result = self.orchestrator.execute(sandbox.id, "sleep 0.2", timeout=0.01)
        self.assertEqual(124, result.exit_code)
        self.assertIn("timed out", result.stderr)

    def test_interrupted_state_is_recovered_on_restart(self) -> None:
        template = self.orchestrator.create_template("recovery")
        sandbox = self.orchestrator.create_sandbox(template.id)
        sandbox.state = SandboxState.SNAPSHOTTING
        self.orchestrator.store.put_sandbox(sandbox)

        reloaded = Orchestrator(self.temporary.name)
        self.assertEqual(
            SandboxState.RUNNING, reloaded.get_sandbox(sandbox.id).state
        )
        recovery = next(
            event
            for event in reloaded.list_events()
            if event.type == "sandbox_recovered"
        )
        self.assertEqual("snapshotting", recovery.details["interrupted_state"])

    def test_timeout_can_pause_and_auto_resume(self) -> None:
        template = self.orchestrator.create_template("auto-resume")
        sandbox = self.orchestrator.create_sandbox(
            template.id,
            timeout_seconds=60,
            timeout_action="pause",
            auto_resume=True,
        )
        sandbox.timeout_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        self.orchestrator.store.put_sandbox(sandbox)
        self.orchestrator.evict_expired()
        self.assertEqual(
            SandboxState.PAUSED,
            self.orchestrator.get_sandbox(sandbox.id).state,
        )

        result = self.orchestrator.execute(sandbox.id, "printf resumed")
        self.assertEqual("resumed", result.stdout)
        resumed = self.orchestrator.get_sandbox(sandbox.id)
        self.assertEqual(SandboxState.RUNNING, resumed.state)
        self.assertIsNotNone(resumed.timeout_at)

    def test_network_policy_validation(self) -> None:
        template = self.orchestrator.create_template("network-validation")
        with self.assertRaises(ValueError):
            self.orchestrator.create_sandbox(
                template.id,
                network=NetworkPolicy(deny_out=["example.com"]),
            )


if __name__ == "__main__":
    unittest.main()
