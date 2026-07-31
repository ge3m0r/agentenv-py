from __future__ import annotations

import base64
import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agentenv.backend import DockerSandboxBackend
from agentenv.commands import CommandConflictError, CommandService
from agentenv.models import Sandbox, SandboxState
from agentenv.orchestrator import Orchestrator


class ManagedCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.orchestrator = Orchestrator(self.temporary.name)
        template = self.orchestrator.create_template("commands")
        self.sandbox = self.orchestrator.create_sandbox(template.id)
        self.commands: CommandService = self.orchestrator.commands

    def tearDown(self) -> None:
        try:
            self.orchestrator.delete(self.sandbox.id)
        except Exception:
            pass
        self.temporary.cleanup()

    def test_background_stream_list_and_reconnect(self) -> None:
        started = time.monotonic()
        command = self.commands.start(
            self.sandbox.id,
            "printf first; sleep 0.15; printf second >&2",
        )
        self.assertLess(time.monotonic() - started, 0.1)

        first = self.commands.read_output(
            self.sandbox.id, command.id, wait_seconds=1
        )
        self.assertEqual("first", first["stdout"])
        self.assertEqual(
            command.id,
            self.commands.connect(
                self.sandbox.id, command_id=command.id
            ).id,
        )
        self.assertEqual(
            command.id,
            self.commands.connect(self.sandbox.id, pid=command.pid).id,
        )
        self.assertIn(
            command.id,
            [item.id for item in self.commands.list(self.sandbox.id)],
        )

        finished = self.commands.wait(self.sandbox.id, command.id, timeout=2)
        self.assertEqual("exited", finished.state)
        self.assertEqual(0, finished.exit_code)
        remaining = self.commands.read_output(
            self.sandbox.id,
            command.id,
            stdout_offset=first["next"]["stdout"],
            stderr_offset=first["next"]["stderr"],
        )
        self.assertEqual("second", remaining["stderr"])

    def test_stdin_and_binary_output(self) -> None:
        command = self.commands.start(
            self.sandbox.id, 'read value; printf "got:%s" "$value"'
        )
        self.commands.send_stdin(
            self.sandbox.id, command.id, "hello\n", encoding="utf-8"
        )
        self.commands.wait(self.sandbox.id, command.id, timeout=2)
        output = self.commands.read_output(self.sandbox.id, command.id)
        self.assertEqual("got:hello", output["stdout"])
        self.assertEqual(
            base64.b64encode(b"got:hello").decode("ascii"),
            output["stdoutBase64"],
        )
        with self.assertRaises(CommandConflictError):
            self.commands.send_stdin(self.sandbox.id, command.id, "again")

    def test_signal_and_foreground_timeout(self) -> None:
        command = self.commands.start(self.sandbox.id, "sleep 10")
        self.commands.signal(self.sandbox.id, command.id, "TERM")
        terminated = self.commands.wait(self.sandbox.id, command.id, timeout=2)
        self.assertEqual("exited", terminated.state)
        self.assertNotEqual(0, terminated.exit_code)

        timed = self.commands.start(self.sandbox.id, "sleep 10")
        result = self.commands.wait(self.sandbox.id, timed.id, timeout=0.02)
        self.assertTrue(result.timed_out)
        self.assertEqual("exited", result.state)

    def test_pause_resume_and_delete_control_processes(self) -> None:
        command = self.commands.start(
            self.sandbox.id,
            'printf start; sleep 0.2; printf end',
        )
        self.orchestrator.pause(self.sandbox.id)
        self.assertEqual(
            "paused", self.commands.get(self.sandbox.id, command.id).state
        )
        self.orchestrator.resume(self.sandbox.id)
        self.assertEqual(
            "running", self.commands.get(self.sandbox.id, command.id).state
        )
        result = self.commands.wait(self.sandbox.id, command.id, timeout=2)
        self.assertEqual(0, result.exit_code)

        sleeping = self.commands.start(self.sandbox.id, "sleep 10")
        pid = sleeping.pid
        self.orchestrator.delete(self.sandbox.id)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 9999
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self):
        return None


class DockerManagedCommandContractTest(unittest.TestCase):
    def test_docker_command_uses_exec_and_runtime_pid_for_signals(self) -> None:
        calls: list[list[str]] = []
        popen_calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        def popen(command, **kwargs):
            popen_calls.append(command)
            return _FakeProcess()

        backend = DockerSandboxBackend(
            pull_missing=False,
            runner=runner,
            popen_factory=popen,
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "rootfs"
            pid_dir = workspace / ".agentenv" / "commands"
            pid_dir.mkdir(parents=True)
            (pid_dir / "cmd_test.pid").write_text("42")
            sandbox = Sandbox(
                id="sbx",
                template_id="tpl",
                workspace_path=str(workspace),
                state=SandboxState.RUNNING,
                backend="docker",
                runtime_id="agentenv-sbx",
            )

            process, pid = backend.start_managed_command(
                sandbox, "cmd_test", "echo hello"
            )
            self.assertEqual(42, pid)
            self.assertEqual("docker", popen_calls[0][0])
            self.assertIn("exec", popen_calls[0])
            self.assertIn("AGENTENV_COMMAND=echo hello", popen_calls[0])

            backend.signal_managed_command(
                sandbox, "cmd_test", process, "TERM"
            )
            self.assertTrue(
                any(
                    command[1:4] == ["exec", "agentenv-sbx", "kill"]
                    and command[-2:] == ["-TERM", "42"]
                    for command in calls
                )
            )


if __name__ == "__main__":
    unittest.main()

