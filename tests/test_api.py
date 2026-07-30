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
        self.assertEqual(204, status)
        self.assertEqual({}, paused)
        self.assertEqual(
            "paused",
            self.api.orchestrator.get_sandbox(sandbox["id"]).state.value,
        )
        status, resumed = self.api.dispatch("POST", f"{sandbox_path}/resume", {})
        self.assertEqual(201, status)
        self.assertEqual("running", resumed["state"])
        status, timeout = self.api.dispatch(
            "POST", f"{sandbox_path}/timeout", {"timeout_seconds": None}
        )
        self.assertEqual(204, status)
        self.assertEqual({}, timeout)

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

    def test_e2b_compatible_create_and_list(self) -> None:
        _, template = self.api.dispatch(
            "POST", "/templates", {"name": "e2b-template"}
        )
        status, sandbox = self.api.dispatch(
            "POST",
            "/sandboxes",
            {
                "templateID": template["id"],
                "timeout": 60,
                "envVars": {"SDK": "e2b"},
                "cpuCount": 2,
                "memoryMB": 256,
                "allow_internet_access": False,
            },
        )
        self.assertEqual(201, status)
        self.assertIn("sandboxID", sandbox)
        self.assertEqual(template["id"], sandbox["templateID"])
        self.assertEqual(2, sandbox["cpuCount"])
        self.assertEqual(256, sandbox["memoryMB"])
        self.assertFalse(sandbox["allowInternetAccess"])

        status, listed = self.api.dispatch("GET", "/v2/sandboxes", {})
        self.assertEqual(200, status)
        self.assertEqual(sandbox["sandboxID"], listed[0]["sandboxID"])

        sandbox_path = f"/sandboxes/{sandbox['sandboxID']}"
        status, updated = self.api.dispatch(
            "PUT",
            f"{sandbox_path}/network",
            {"allowInternetAccess": True, "allowOut": ["example.com"]},
        )
        self.assertEqual(200, status)
        self.assertEqual(["example.com"], updated["network"]["allowOut"])
        status, updated = self.api.dispatch(
            "PUT",
            f"{sandbox_path}/resources",
            {"cpuCount": 1.5, "memoryMB": 512, "pidsLimit": 64},
        )
        self.assertEqual(200, status)
        self.assertEqual(1.5, updated["cpuCount"])

        self.api.dispatch("POST", f"{sandbox_path}/pause", {})
        status, paused = self.api.dispatch(
            "GET", "/v2/sandboxes?state=paused", {}
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(paused))

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
