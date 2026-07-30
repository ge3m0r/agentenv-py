from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentenv.backend import DockerSandboxBackend
from agentenv.models import NetworkPolicy, ResourceLimits
from agentenv.oci import OCIReference, OCIReferenceError
from agentenv.orchestrator import Orchestrator


class OCIReferenceTest(unittest.TestCase):
    def test_normalizes_docker_hub_references(self) -> None:
        self.assertEqual(
            "docker.io/library/ubuntu:22.04",
            OCIReference.parse("ubuntu:22.04").canonical,
        )
        self.assertEqual(
            "ghcr.io/acme/agent:latest",
            OCIReference.parse("ghcr.io/acme/agent").canonical,
        )

    def test_supports_digest_and_rejects_invalid_reference(self) -> None:
        digest = "sha256:" + ("a" * 64)
        self.assertEqual(
            f"registry.example.com/team/image@{digest}",
            OCIReference.parse(
                f"registry.example.com/team/image@{digest}"
            ).canonical,
        )
        with self.assertRaises(OCIReferenceError):
            OCIReference.parse("Bad Image")


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[1:3] == ["image", "inspect"]:
            payload = [
                {
                    "Id": "sha256:image",
                    "RepoDigests": [
                        "docker.io/library/alpine@sha256:" + ("b" * 64)
                    ],
                    "Config": {"Env": ["PATH=/bin"], "WorkingDir": "/"},
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[1] == "exec":
            return subprocess.CompletedProcess(command, 0, "docker-ok\n", "")
        if command[1:3] == ["inspect", "--format"]:
            return subprocess.CompletedProcess(command, 0, "true\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class DockerBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.docker = FakeDocker()
        self.backend = DockerSandboxBackend(
            pull_missing=False, runner=self.docker
        )
        self.orchestrator = Orchestrator(
            self.temporary.name, backend=self.backend
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_docker_lifecycle_and_limits(self) -> None:
        template = self.orchestrator.create_template(
            "alpine", source="alpine:3.20"
        )
        self.assertEqual(
            "docker.io/library/alpine:3.20", template.image_ref
        )
        sandbox = self.orchestrator.create_sandbox(
            template.id,
            resources=ResourceLimits(
                cpu_count=2,
                memory_mb=256,
                disk_size_mb=1024,
                pids_limit=64,
            ),
            network=NetworkPolicy(allow_internet_access=False),
        )
        create = next(
            command for command in self.docker.commands if command[1] == "create"
        )
        self.assertIn("--cpus", create)
        self.assertIn("2", create)
        self.assertIn("256m", create)
        self.assertIn("none", create)

        result = self.orchestrator.execute(sandbox.id, "echo ok")
        self.assertEqual("docker-ok\n", result.stdout)
        self.orchestrator.pause(sandbox.id)
        self.orchestrator.resume(sandbox.id)
        self.orchestrator.update_resources(
            sandbox.id,
            ResourceLimits(cpu_count=1, memory_mb=128, pids_limit=32),
        )
        self.orchestrator.update_network(
            sandbox.id, NetworkPolicy(allow_internet_access=True)
        )
        self.assertTrue(
            any(command[1] == "pause" for command in self.docker.commands)
        )
        self.assertTrue(
            any(command[1] == "unpause" for command in self.docker.commands)
        )
        self.assertTrue(
            any(command[1] == "update" for command in self.docker.commands)
        )
        self.assertTrue(
            any(command[1:3] == ["network", "connect"] for command in self.docker.commands)
        )

        snapshot = self.orchestrator.snapshot(sandbox.id)
        self.assertEqual("docker", snapshot.backend)
        self.assertTrue(Path(snapshot.rootfs_path).exists())
        restored = self.orchestrator.create_sandbox(snapshot_id=snapshot.id)
        self.assertEqual("docker", restored.backend)
        self.orchestrator.delete(restored.id)


if __name__ == "__main__":
    unittest.main()
