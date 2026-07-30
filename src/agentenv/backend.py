from __future__ import annotations

import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from .models import CommandResult, Sandbox, Snapshot, Template


class SandboxBackend(ABC):
    """The replaceable runtime boundary; Firecracker can be added behind this."""

    @abstractmethod
    def create(self, template: Template, sandbox: Sandbox) -> None: ...

    @abstractmethod
    def execute(
        self, sandbox: Sandbox, command: str, timeout: float | None = None
    ) -> CommandResult: ...

    @abstractmethod
    def capture(self, sandbox: Sandbox, destination: Path) -> None: ...

    @abstractmethod
    def restore(self, snapshot: Snapshot, sandbox: Sandbox) -> None: ...

    @abstractmethod
    def destroy(self, sandbox: Sandbox) -> None: ...


class LocalProcessBackend(SandboxBackend):
    """
    Development backend using a copied directory and local subprocesses.

    It demonstrates the lifecycle but is NOT a security sandbox: commands run
    with the current user's permissions.
    """

    def create(self, template: Template, sandbox: Sandbox) -> None:
        source = Path(template.rootfs_path)
        destination = Path(sandbox.workspace_path)
        if destination.exists():
            raise FileExistsError(f"workspace already exists: {destination}")
        if source.exists():
            shutil.copytree(source, destination)
        else:
            destination.mkdir(parents=True)

    def execute(
        self, sandbox: Sandbox, command: str, timeout: float | None = None
    ) -> CommandResult:
        root = Path(sandbox.workspace_path).resolve()
        working_directory = (root / sandbox.workdir).resolve()
        if root != working_directory and root not in working_directory.parents:
            raise ValueError("workdir must stay inside the sandbox workspace")
        working_directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(sandbox.env)
        started = time.monotonic()
        executed_at = datetime.now(timezone.utc).isoformat()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=working_directory,
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            exit_code, stdout, stderr = (
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        except subprocess.TimeoutExpired as error:
            exit_code = 124
            stdout = error.stdout or ""
            stderr = error.stderr or f"command timed out after {timeout} seconds"
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round((time.monotonic() - started) * 1000),
            executed_at=executed_at,
        )

    def capture(self, sandbox: Sandbox, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(f"snapshot already exists: {destination}")
        shutil.copytree(sandbox.workspace_path, destination)

    def restore(self, snapshot: Snapshot, sandbox: Sandbox) -> None:
        destination = Path(sandbox.workspace_path)
        if destination.exists():
            raise FileExistsError(f"workspace already exists: {destination}")
        shutil.copytree(snapshot.rootfs_path, destination)

    def destroy(self, sandbox: Sandbox) -> None:
        workspace = Path(sandbox.workspace_path)
        if workspace.exists():
            shutil.rmtree(workspace)
