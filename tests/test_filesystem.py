from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from agentenv.backend import DockerSandboxBackend, LocalProcessBackend
from agentenv.filesystem import (
    FilesystemConflictError,
    FilesystemNotFoundError,
    FilesystemPathError,
    FilesystemService,
)
from agentenv.models import Sandbox, SandboxState
from agentenv.orchestrator import AgentEnvError, Orchestrator


def _sandbox(root: Path, backend: str) -> Sandbox:
    return Sandbox(
        id=f"sbx_{backend}",
        template_id="tpl",
        workspace_path=str(root),
        state=SandboxState.RUNNING,
        backend=backend,
    )


class FilesystemBackendContractTest(unittest.TestCase):
    def test_local_and_docker_share_the_filesystem_contract(self) -> None:
        for backend in (LocalProcessBackend(), DockerSandboxBackend()):
            with self.subTest(backend=backend.name):
                with tempfile.TemporaryDirectory() as directory:
                    filesystem = backend.filesystem(
                        _sandbox(Path(directory) / "rootfs", backend.name)
                    )
                    created = filesystem.make_dir("/project/src")
                    self.assertEqual("directory", created.type)

                    written = filesystem.write(
                        "/project/src/main.py", "print('hello')\n"
                    )
                    self.assertEqual("file", written.type)
                    self.assertEqual(
                        "print('hello')\n",
                        filesystem.read("/project/src/main.py")["data"],
                    )

                    entries = filesystem.list("/project/src")
                    self.assertEqual(["main.py"], [entry.name for entry in entries])

                    moved = filesystem.move(
                        "/project/src/main.py", "/project/app.py"
                    )
                    self.assertEqual("/project/app.py", moved.path)
                    with self.assertRaises(FilesystemNotFoundError):
                        filesystem.stat("/project/src/main.py")

                    filesystem.remove("/project", recursive=True)
                    with self.assertRaises(FilesystemNotFoundError):
                        filesystem.stat("/project")

    def test_binary_round_trip_and_stable_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            filesystem = LocalProcessBackend().filesystem(
                _sandbox(Path(directory) / "rootfs", "local")
            )
            payload = b"\x00\xffagentenv\n"
            encoded = base64.b64encode(payload).decode("ascii")
            filesystem.write("/payload.bin", encoded, encoding="base64")
            result = filesystem.read("/payload.bin", encoding="base64")
            self.assertEqual(encoded, result["data"])
            self.assertEqual(len(payload), result["size"])
            with self.assertRaises(FilesystemConflictError):
                filesystem.read("/payload.bin", encoding="utf-8")
            with self.assertRaises(FilesystemConflictError):
                filesystem.list("/payload.bin")
            with self.assertRaises(FilesystemConflictError):
                filesystem.write("/payload.bin/child", "invalid")
            with self.assertRaises(FilesystemConflictError):
                filesystem.make_dir("/payload.bin/child")

    def test_paths_and_symlinks_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "rootfs"
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret")
            filesystem = LocalProcessBackend().filesystem(
                _sandbox(root, "local")
            )
            with self.assertRaises(FilesystemPathError):
                filesystem.read("../outside.txt")
            with self.assertRaises(FilesystemPathError):
                filesystem.write("/safe/../../outside.txt", "changed")

            root.mkdir(parents=True, exist_ok=True)
            (root / "escape").symlink_to(outside)
            with self.assertRaises(FilesystemPathError):
                filesystem.read("/escape")
            self.assertEqual("secret", outside.read_text())


class FilesystemServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.orchestrator = Orchestrator(self.temporary.name)
        template = self.orchestrator.create_template("filesystem")
        self.sandbox = self.orchestrator.create_sandbox(template.id)
        self.service = FilesystemService(self.orchestrator)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_service_uses_sandbox_lifecycle(self) -> None:
        self.service.write(self.sandbox.id, "/hello.txt", "hello")
        self.orchestrator.pause(self.sandbox.id)
        with self.assertRaises(AgentEnvError):
            self.service.read(self.sandbox.id, "/hello.txt")

    def test_filesystem_activity_auto_resumes(self) -> None:
        sandbox = self.orchestrator.get_sandbox(self.sandbox.id)
        sandbox.timeout_action = "pause"
        sandbox.auto_resume = True
        sandbox.timeout_seconds = 60
        self.orchestrator.store.put_sandbox(sandbox)
        self.service.write(sandbox.id, "/hello.txt", "hello")
        self.orchestrator.pause(sandbox.id)

        result = self.service.read(sandbox.id, "/hello.txt")
        self.assertEqual("hello", result["data"])
        self.assertEqual(
            SandboxState.RUNNING,
            self.orchestrator.get_sandbox(sandbox.id).state,
        )


if __name__ == "__main__":
    unittest.main()
