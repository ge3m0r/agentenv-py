from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from agentenv.api import AgentEnvApi, MaintenanceWorker
from agentenv.orchestrator import Orchestrator


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.api = AgentEnvApi(Orchestrator(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_api_lifecycle(self) -> None:
        status, template = self.api.dispatch(
            "POST", "/templates", {"name": "api-template"}
        )
        self.assertEqual(201, status)
        status, sandbox = self.api.dispatch(
            "POST",
            "/sandboxes",
            {"template_id": template["id"], "timeout_seconds": 60},
        )
        self.assertEqual(201, status)

        sandbox_path = f"/sandboxes/{sandbox['id']}"
        status, result = self.api.dispatch(
            "POST", f"{sandbox_path}/exec", {"command": "printf api"}
        )
        self.assertEqual(200, status)
        self.assertEqual("api", result["stdout"])

        status, paused = self.api.dispatch("POST", f"{sandbox_path}/pause", {})
        self.assertEqual("paused", paused["state"])
        status, resumed = self.api.dispatch("POST", f"{sandbox_path}/resume", {})
        self.assertEqual("running", resumed["state"])
        status, timeout = self.api.dispatch(
            "POST", f"{sandbox_path}/timeout", {"timeout_seconds": None}
        )
        self.assertIsNone(timeout["timeout_at"])

        status, snapshot = self.api.dispatch(
            "POST", f"{sandbox_path}/snapshots", {}
        )
        self.assertEqual(201, status)
        status, fetched = self.api.dispatch(
            "GET", f"/snapshots/{snapshot['id']}", {}
        )
        self.assertEqual(snapshot["id"], fetched["id"])

        status, events = self.api.dispatch("GET", "/events?limit=3", {})
        self.assertEqual(200, status)
        self.assertEqual(3, len(events))
        status, summary = self.api.dispatch("GET", "/status", {})
        self.assertEqual(1, summary["sandboxes"])

    def test_unknown_route(self) -> None:
        status, body = self.api.dispatch("GET", "/missing", {})
        self.assertEqual(404, status)
        self.assertIn("error", body)

    def test_maintenance_worker_evicts_expired_sandbox(self) -> None:
        orchestrator = self.api.orchestrator
        template = orchestrator.create_template("maintenance")
        sandbox = orchestrator.create_sandbox(template.id)
        sandbox.timeout_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        orchestrator.store.put_sandbox(sandbox)

        worker = MaintenanceWorker(orchestrator, interval=0.01)
        worker.start()
        deadline = time.monotonic() + 1
        while orchestrator.list_sandboxes() and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop()
        self.assertEqual([], orchestrator.list_sandboxes())


if __name__ == "__main__":
    unittest.main()
