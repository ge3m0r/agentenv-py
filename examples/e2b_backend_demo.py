"""End-to-end demo of the E2B backend through the orchestrator.

Run it with::

    # 1. install the optional E2B dependency
    pip install -e ".[e2b]"

    # 2. put your key in .env  (E2B_API_KEY=...)
    # 3. run
    PYTHONPATH=src python examples/e2b_backend_demo.py

What it does
------------
1. Creates an E2B "base" template record (local metadata only — no API call).
2. Starts a sandbox on E2B.
3. Runs a command in it (writes a file, reads it back).
4. Pauses and resumes (connect) the sandbox.
5. Snapshots the running sandbox (cloud-side E2B snapshot).
6. Restores a second sandbox from that snapshot and reads the file back.
7. Tears everything down, including the cloud snapshot.

This exercises every E2BSandboxBackend method against the real E2B API.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make `agentenv` importable when running the example directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agentenv.e2b_backend import E2BSandboxBackend
from agentenv.orchestrator import Orchestrator


def _print(label: str, value: object) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(f"\n== {label} ==")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    if not os.environ.get("E2B_API_KEY"):
        print("error: E2B_API_KEY is not set (put it in .env)", file=sys.stderr)
        return 1

    backend = E2BSandboxBackend()
    orchestrator = Orchestrator(Path(".agentenv-e2b-demo"), backend=backend)

    template = orchestrator.create_template("e2b-base", source="base")
    _print("template", template)

    sandbox = orchestrator.create_sandbox(
        template.id,
        timeout_seconds=300,
        env={"MESSAGE": "hello from e2b"},
        metadata={"demo": "e2b-backend"},
    )
    _print("sandbox", sandbox)

    first = orchestrator.execute(
        sandbox.id,
        'echo "$MESSAGE" > result.txt && cat result.txt',
        timeout=30,
    )
    _print("first command", first)

    paused = orchestrator.pause(sandbox.id)
    _print("paused", paused)
    resumed = orchestrator.resume(sandbox.id)
    _print("resumed", resumed)

    snapshot = orchestrator.snapshot(sandbox.id)
    _print("snapshot", snapshot)

    restored = orchestrator.create_sandbox(snapshot_id=snapshot.id, timeout_seconds=300)
    _print("restored sandbox", restored)
    second = orchestrator.execute(restored.id, "cat result.txt", timeout=30)
    _print("restored command", second)

    # Clean up — delete_snapshot also removes the cloud-side E2B snapshot.
    orchestrator.delete(restored.id)
    orchestrator.delete_snapshot(snapshot.id)
    orchestrator.delete(sandbox.id)
    orchestrator.delete_template(template.id)

    print("\n== status ==")
    print(json.dumps(orchestrator.status(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
