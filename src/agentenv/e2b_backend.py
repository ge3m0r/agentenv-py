"""E2B hosted sandbox backend.

This backend runs sandboxes on `E2B <https://e2b.dev>`__ instead of on the
local machine or Docker. It implements the same :class:`SandboxBackend`
contract as :class:`LocalProcessBackend` and :class:`DockerSandboxBackend`, so
the orchestrator, HTTP API and CLI work unchanged.

The E2B SDK (``e2b``) is an *optional* dependency: it is only needed when the
``e2b`` backend is selected. Install it with the ``e2b`` extra::

    pip install -e ".[e2b]"

and provide an API key via the ``E2B_API_KEY`` environment variable.

Mapping notes
-------------
- The E2B ``sandbox_id`` is stored in :attr:`Sandbox.runtime_id`, so handles
  can be reconstituted across operations and process restarts using only the
  persisted id plus the API key.
- E2B resources (CPU/memory) are fixed by the template and cannot be changed
  at runtime; :meth:`update_resources` is a no-op that still lets the
  orchestrator persist the requested limits as metadata.
- Snapshots are cloud-side E2B snapshots. Their id is recorded in a small
  ``e2b_snapshot.json`` sidecar under the snapshot's ``rootfs_path`` directory,
  and :meth:`restore` boots a new sandbox from it.
- Cold start from an OCI image is a Docker-only concept; with E2B you start
  sandboxes from E2B *templates* (``template.source`` holds the template name,
  ``scratch``/empty falls back to E2B's default ``base`` template).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .backend import SandboxBackend
from .models import CommandResult, NetworkPolicy, Sandbox, Snapshot, Template

# The E2B SDK is optional. Import it lazily-guarded so this module can be
# imported (e.g. by tests) even when e2b is not installed.
try:  # pragma: no cover - exercised only when e2b is installed
    from e2b import Sandbox as _E2BSandbox
    from e2b import SandboxNotFoundException as _E2BNotFound
    from e2b import TimeoutException as _E2BTimeout

    _E2B_AVAILABLE = True
except ImportError:  # pragma: no cover
    _E2BSandbox = None  # type: ignore[assignment]
    _E2BNotFound = ()  # type: ignore[assignment]
    _E2BTimeout = ()  # type: ignore[assignment]
    _E2B_AVAILABLE = False

# Exceptions to treat as "sandbox not found" / "command timed out". Empty
# tuples catch nothing, which keeps the guards safe when e2b is absent.
_NOT_FOUND_EXCS: tuple = (_E2BNotFound,) if _E2B_AVAILABLE else ()
_TIMEOUT_EXCS: tuple = (_E2BTimeout,) if _E2B_AVAILABLE else ()

_SNAPSHOT_META = "e2b_snapshot.json"
_SCRATCH_SOURCES = ("", "scratch", None)


class E2BBackendError(RuntimeError):
    """Raised for misconfiguration of the E2B backend (e.g. missing API key)."""


class E2BSandboxBackend(SandboxBackend):
    """Runs sandboxes on E2B's hosted runtime.

    Parameters
    ----------
    sandbox_class:
        Injectable E2B ``Sandbox`` class. Production code leaves this as
        ``None`` to use the real SDK; tests pass a stub to assert the mapping
        without touching the network.
    api_key:
        Explicit API key. When ``None`` the SDK reads ``E2B_API_KEY`` from the
        environment, matching the standard E2B SDK behaviour.
    """

    name = "e2b"

    def __init__(
        self,
        *,
        sandbox_class: Any = None,
        api_key: str | None = None,
    ) -> None:
        if sandbox_class is None:
            if not _E2B_AVAILABLE:
                raise E2BBackendError(
                    "the 'e2b' package is required for the e2b backend; "
                    "install it with: pip install -e \".[e2b]\""
                )
            sandbox_class = _E2BSandbox
            # Convenience: load a .env file if python-dotenv is available, so the
            # documented `E2B_API_KEY=...` in .env works for CLI usage too. Only
            # done on the real-SDK path so injected test stubs stay hermetic.
            try:  # pragma: no cover - depends on optional dotenv presence
                from dotenv import load_dotenv

                load_dotenv()
            except ImportError:
                pass
        self._sandbox_cls = sandbox_class
        self._api_key = api_key or os.environ.get("E2B_API_KEY")
        if not self._api_key:
            raise E2BBackendError(
                "E2B_API_KEY is not set; provide it via the environment or "
                "the E2B_API_KEY variable in your .env file"
            )

    # -- helpers ---------------------------------------------------------

    def _opts(self) -> dict[str, Any]:
        """Connection options forwarded to every SDK call."""
        return {"api_key": self._api_key} if self._api_key else {}

    @staticmethod
    def _network_opts(policy: NetworkPolicy) -> dict[str, Any] | None:
        opts: dict[str, Any] = {}
        if policy.allow_out:
            opts["allow_out"] = list(policy.allow_out)
        if policy.deny_out:
            opts["deny_out"] = list(policy.deny_out)
        return opts or None

    @staticmethod
    def _network_update(policy: NetworkPolicy) -> dict[str, Any]:
        update: dict[str, Any] = {
            "allow_internet_access": policy.allow_internet_access
        }
        if policy.allow_out:
            update["allow_out"] = list(policy.allow_out)
        if policy.deny_out:
            update["deny_out"] = list(policy.deny_out)
        return update

    @staticmethod
    def _lifecycle_opts(
        timeout_action: str, auto_resume: bool
    ) -> dict[str, Any] | None:
        if timeout_action == "kill" and not auto_resume:
            return None  # E2B default
        return {"on_timeout": timeout_action, "auto_resume": auto_resume}

    @staticmethod
    def _template_source(template: Template) -> str | None:
        source = (template.source or "").strip()
        if source in _SCRATCH_SOURCES:
            return None  # E2B default "base" template
        return source

    @staticmethod
    def _cwd(sandbox: Sandbox) -> str | None:
        workdir = (sandbox.workdir or "").strip()
        if not workdir or workdir == ".":
            return None
        return workdir

    def _runtime_id(self, sandbox: Sandbox) -> str:
        if not sandbox.runtime_id:
            raise E2BBackendError(
                f"sandbox {sandbox.id} has no E2B sandbox id"
            )
        return sandbox.runtime_id

    def _create_kwargs(self, sandbox: Sandbox, template: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "template": template,
            "timeout": sandbox.timeout_seconds,
            "envs": sandbox.env or None,
            "metadata": sandbox.metadata or None,
            "allow_internet_access": sandbox.network.allow_internet_access,
            "network": self._network_opts(sandbox.network),
            "lifecycle": self._lifecycle_opts(
                sandbox.timeout_action, sandbox.auto_resume
            ),
        }
        return kwargs

    # -- SandboxBackend implementation -----------------------------------

    def prepare_template(self, template: Template) -> Template:
        # E2B templates are built out-of-band; ``source`` is the template name.
        # ``scratch``/empty means "use the default base template".
        if template.source not in _SCRATCH_SOURCES:
            template.image_ref = template.source
        return template

    def create(self, template: Template, sandbox: Sandbox) -> None:
        sbx = self._sandbox_cls.create(
            **self._create_kwargs(sandbox, self._template_source(template)),
            **self._opts(),
        )
        sandbox.runtime_id = sbx.sandbox_id
        sandbox.image_ref = template.source or "base"

    def execute(
        self, sandbox: Sandbox, command: str, timeout: float | None = None
    ) -> CommandResult:
        sbx = self._sandbox_cls.connect(
            sandbox_id=self._runtime_id(sandbox), **self._opts()
        )
        started = time.monotonic()
        executed_at = datetime.now(timezone.utc).isoformat()
        try:
            result = sbx.commands.run(
                cmd=command,
                envs=sandbox.env or None,
                cwd=self._cwd(sandbox),
                timeout=timeout,
            )
            exit_code, stdout, stderr = (
                result.exit_code,
                result.stdout,
                result.stderr,
            )
        except _TIMEOUT_EXCS as error:  # type: ignore[misc]
            exit_code = 124
            stdout = ""
            stderr = f"command timed out after {timeout} seconds" + (
                f" ({error})" if str(error) else ""
            )
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

    def pause(self, sandbox: Sandbox) -> None:
        self._sandbox_cls.pause(
            sandbox_id=self._runtime_id(sandbox), keep_memory=True, **self._opts()
        )

    def resume(self, sandbox: Sandbox) -> None:
        # connect() resumes a paused sandbox and is a no-op for a running one.
        self._sandbox_cls.connect(
            sandbox_id=self._runtime_id(sandbox), **self._opts()
        )

    def capture(self, sandbox: Sandbox, destination: Path) -> None:
        snapshot = self._sandbox_cls.create_snapshot(
            sandbox_id=self._runtime_id(sandbox), name=sandbox.id, **self._opts()
        )
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / _SNAPSHOT_META).write_text(
            json.dumps({"e2b_snapshot_id": snapshot.snapshot_id})
        )

    def restore(self, snapshot: Snapshot, sandbox: Sandbox) -> None:
        e2b_snapshot_id = self._read_snapshot_id(snapshot)
        sbx = self._sandbox_cls.create(
            **self._create_kwargs(sandbox, e2b_snapshot_id),
            **self._opts(),
        )
        sandbox.runtime_id = sbx.sandbox_id
        sandbox.image_ref = e2b_snapshot_id

    def update_network(self, sandbox: Sandbox, policy: NetworkPolicy) -> None:
        self._sandbox_cls.update_network(
            sandbox_id=self._runtime_id(sandbox),
            network=self._network_update(policy),
            **self._opts(),
        )

    def update_resources(
        self, sandbox: Sandbox, resources: "Any"
    ) -> None:
        # E2B resource limits are fixed by the template and immutable at
        # runtime; accept and let the orchestrator persist them as metadata.
        return None

    def runtime_alive(self, sandbox: Sandbox) -> bool:
        try:
            self._sandbox_cls.get_info(
                sandbox_id=self._runtime_id(sandbox), **self._opts()
            )
            return True
        except _NOT_FOUND_EXCS:  # type: ignore[misc]
            return False

    def destroy(self, sandbox: Sandbox) -> None:
        # kill() returns False when the sandbox is already gone, which is fine.
        self._sandbox_cls.kill(
            sandbox_id=self._runtime_id(sandbox), **self._opts()
        )

    def delete_snapshot(self, snapshot: Snapshot) -> None:
        """Delete the cloud-side E2B snapshot (best-effort)."""
        e2b_snapshot_id = self._read_snapshot_id(snapshot, missing_ok=True)
        if not e2b_snapshot_id:
            return
        try:
            self._sandbox_cls.delete_snapshot(
                snapshot_id=e2b_snapshot_id, **self._opts()
            )
        except _NOT_FOUND_EXCS:  # type: ignore[misc]
            return

    # -- snapshot sidecar ------------------------------------------------

    @staticmethod
    def _read_snapshot_id(
        snapshot: Snapshot, *, missing_ok: bool = False
    ) -> str | None:
        meta_path = Path(snapshot.rootfs_path) / _SNAPSHOT_META
        if not meta_path.exists():
            if missing_ok:
                return None
            raise E2BBackendError(
                f"snapshot {snapshot.id} is missing E2B metadata at {meta_path}"
            )
        return json.loads(meta_path.read_text()).get("e2b_snapshot_id")
