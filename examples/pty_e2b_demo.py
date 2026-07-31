"""Real E2B PTY end-to-end demo (no HTTP server needed).

Exercises the E2B PTY data plane through the orchestrator:
start -> send input -> read streamed output -> resize -> kill -> wait.
Run: PYTHONPATH=src python examples/pty_e2b_demo.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agentenv.e2b_backend import E2BSandboxBackend
from agentenv.orchestrator import Orchestrator


def main() -> int:
    if not os.environ.get("E2B_API_KEY"):
        print("error: E2B_API_KEY is not set (put it in .env)", file=sys.stderr)
        return 1
    backend = E2BSandboxBackend()
    orch = Orchestrator(Path(".agentenv-e2b-pty-demo"), backend=backend)

    template = orch.create_template("e2b-pty", source="base")
    sandbox = orch.create_sandbox(template.id, timeout_seconds=300)
    print(f"sandbox: {sandbox.id} (runtime {sandbox.runtime_id})")

    info = orch.pty.start(sandbox.id, rows=24, cols=80)
    print(f"pty: {info.id} pid={info.pid}")

    orch.pty.send_input(sandbox.id, info.id, b"echo hello-from-pty\n")

    seen = None
    for _ in range(30):
        time.sleep(0.5)
        out = orch.pty.read_output(sandbox.id, info.id)
        if "hello-from-pty" in out["data"]:
            seen = out["data"]
            break
    print("output contains marker:", seen is not None)
    print("output sample:", repr((seen or out["data"])[:200]))

    orch.pty.resize(sandbox.id, info.id, 40, 120)
    print("resized to 40x120")

    orch.pty.kill(sandbox.id, info.id)
    orch.pty.wait(sandbox.id, info.id, timeout=10)
    print("pty exited:", orch.pty.get(sandbox.id, info.id).state)

    orch.delete(sandbox.id)
    orch.delete_template(template.id)
    print("cleaned up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
