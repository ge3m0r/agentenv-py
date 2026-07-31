"""Tests for the PTY service and the stdlib WebSocket codec.

The backend is faked so the tests run anywhere (including Windows, where the
local PTY is unavailable) without touching the network or real terminals. The
WebSocket codec is checked against the RFC 6455 reference vectors.
"""

from __future__ import annotations

import io
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentenv.backend import SandboxBackend
from agentenv.models import (
    CommandResult,
    NetworkPolicy,
    ResourceLimits,
    Sandbox,
    Snapshot,
    Template,
)
from agentenv.orchestrator import Orchestrator
from agentenv.pty import (
    PtyConflictError,
    PtyNotFoundError,
    PtyService,
    PtyUnavailableError,
)
from agentenv.pty_ws import (
    OPCODE_BINARY,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocket,
    websocket_accept_key,
)


class FakePtyHandle:
    def __init__(self) -> None:
        self.input = bytearray()
        self.killed = False
        self.resizes: list[tuple[int, int]] = []


class FakePtyBackend(SandboxBackend):
    """A backend whose only real behaviour is the PTY data plane."""

    name = "fake-pty"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._counter = 0
        self._lock = threading.Lock()

    def prepare_template(self, template: Template) -> Template:
        template.image_ref = template.source
        return template

    def create(self, template: Template, sandbox: Sandbox) -> None:
        Path(sandbox.workspace_path).mkdir(parents=True, exist_ok=True)

    def execute(self, sandbox, command, timeout=None) -> CommandResult:
        return CommandResult(
            command=command, exit_code=0, stdout="", stderr="",
            duration_ms=0, executed_at="",
        )

    def pause(self, sandbox) -> None:
        return None

    def resume(self, sandbox) -> None:
        return None

    def capture(self, sandbox, destination) -> None:
        destination.mkdir(parents=True, exist_ok=True)

    def restore(self, snapshot, sandbox) -> None:
        Path(sandbox.workspace_path).mkdir(parents=True, exist_ok=True)

    def update_network(self, sandbox, policy) -> None:
        return None

    def update_resources(self, sandbox, resources) -> None:
        return None

    def runtime_alive(self, sandbox) -> bool:
        return True

    def destroy(self, sandbox) -> None:
        return None

    def start_pty(self, sandbox, pty_id, *, rows, cols, command, cwd, envs,
                  on_output, on_exit):
        handle = FakePtyHandle()
        with self._lock:
            self._counter += 1
            pid = self._counter
        # Record the start call before spawning the reader so ordering is
        # deterministic with respect to the async cleanup callback.
        self.calls.append(("start", pty_id, rows, cols, command))

        def reader() -> None:
            on_output(b"boot\n")
            on_output(b"prompt$ ")
            on_exit(0)

        threading.Thread(target=reader, name=f"{pty_id}-fake", daemon=True).start()
        return handle, pid

    def send_pty_input(self, sandbox, handle, data):
        handle.input.extend(data)

    def resize_pty(self, sandbox, handle, rows, cols):
        handle.resizes.append((rows, cols))

    def kill_pty(self, sandbox, handle):
        handle.killed = True

    def cleanup_pty(self, sandbox, pty_id):
        self.calls.append(("cleanup", pty_id))


class LiveFakePtyBackend(FakePtyBackend):
    """A fake backend whose PTY never exits on its own (no reader thread)."""

    def __init__(self) -> None:
        super().__init__()
        self.handles: dict[str, FakePtyHandle] = {}

    def start_pty(self, sandbox, pty_id, *, rows, cols, command, cwd, envs,
                  on_output, on_exit):
        handle = FakePtyHandle()
        with self._lock:
            self._counter += 1
            pid = self._counter
        self.handles[pty_id] = handle
        self.calls.append(("start", pty_id, rows, cols, command))
        return handle, pid


class NoPtyBackend(SandboxBackend):
    """A backend with no PTY support (inherits ABC defaults)."""

    name = "no-pty"

    def prepare_template(self, template: Template) -> Template:
        template.image_ref = template.source
        return template

    def create(self, template: Template, sandbox: Sandbox) -> None:
        Path(sandbox.workspace_path).mkdir(parents=True, exist_ok=True)

    def execute(self, sandbox, command, timeout=None) -> CommandResult:
        return CommandResult(
            command=command, exit_code=0, stdout="", stderr="",
            duration_ms=0, executed_at="",
        )

    def pause(self, sandbox) -> None:
        return None

    def resume(self, sandbox) -> None:
        return None

    def capture(self, sandbox, destination) -> None:
        destination.mkdir(parents=True, exist_ok=True)

    def restore(self, snapshot, sandbox) -> None:
        Path(sandbox.workspace_path).mkdir(parents=True, exist_ok=True)

    def update_network(self, sandbox, policy) -> None:
        return None

    def update_resources(self, sandbox, resources) -> None:
        return None

    def runtime_alive(self, sandbox) -> bool:
        return True

    def destroy(self, sandbox) -> None:
        return None


class PtyServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.backend = FakePtyBackend()
        self.orchestrator = Orchestrator(self.temporary.name, backend=self.backend)
        template = self.orchestrator.create_template("pty-demo", source="scratch")
        self.sandbox = self.orchestrator.create_sandbox(template.id)
        self.pty: PtyService = self.orchestrator.pty

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_streams_output_and_records_exit(self) -> None:
        info = self.pty.start(self.sandbox.id, rows=30, cols=100)
        self.assertTrue(info.id.startswith("pty_"))
        self.assertEqual(30, info.rows)
        self.assertEqual(100, info.cols)
        self.assertIn(("start", info.id, 30, 100, None), self.backend.calls)

        finished = self.pty.wait(self.sandbox.id, info.id, timeout=2)
        self.assertEqual("exited", finished.state)
        self.assertEqual(0, finished.exit_code)
        self.assertIn(("cleanup", info.id), self.backend.calls)

        output = self.pty.read_output(self.sandbox.id, info.id)
        self.assertIn("boot", output["data"])
        self.assertIn("prompt$ ", output["data"])

    def test_input_to_exited_pty_is_conflict(self) -> None:
        info = self.pty.start(self.sandbox.id)
        self.pty.wait(self.sandbox.id, info.id, timeout=2)
        with self.assertRaises(PtyConflictError):
            self.pty.send_input(self.sandbox.id, info.id, b"x")

    def test_send_input_resize_and_kill_live_session(self) -> None:
        backend = LiveFakePtyBackend()
        orch = Orchestrator(tempfile.mkdtemp(), backend=backend)
        template = orch.create_template("live", source="scratch")
        sandbox = orch.create_sandbox(template.id)
        info = orch.pty.start(sandbox.id, rows=24, cols=80)
        orch.pty.send_input(sandbox.id, info.id, b"ls\n")
        updated = orch.pty.resize(sandbox.id, info.id, 40, 120)
        self.assertEqual((40, 120), (updated.rows, updated.cols))
        orch.pty.kill(sandbox.id, info.id)
        handle = backend.handles[info.id]
        self.assertEqual(b"ls\n", bytes(handle.input))
        self.assertEqual([(40, 120)], handle.resizes)
        self.assertTrue(handle.killed)

    def test_reconnect_via_offset(self) -> None:
        info = self.pty.start(self.sandbox.id)
        self.pty.wait(self.sandbox.id, info.id, timeout=2)
        first = self.pty.read_output(self.sandbox.id, info.id)
        second = self.pty.read_output(
            self.sandbox.id, info.id, offset=first["next"]
        )
        self.assertEqual("", second["data"])

    def test_unknown_pty_raises(self) -> None:
        with self.assertRaises(PtyNotFoundError):
            self.pty.get(self.sandbox.id, "pty_missing")

    def test_terminate_sandbox_cleans_sessions(self) -> None:
        info = self.pty.start(self.sandbox.id)
        self.pty.wait(self.sandbox.id, info.id, timeout=2)
        self.assertEqual(1, len(self.pty.list(self.sandbox.id)))
        self.orchestrator.delete(self.sandbox.id)
        with self.assertRaises(Exception):
            self.pty.list(self.sandbox.id)

    def test_unavailable_backend_raises(self) -> None:
        backend = NoPtyBackend()
        orch = Orchestrator(tempfile.mkdtemp(), backend=backend)
        template = orch.create_template("nopty", source="scratch")
        sandbox = orch.create_sandbox(template.id)
        with self.assertRaises(PtyUnavailableError):
            orch.pty.start(sandbox.id)


# -- WebSocket codec tests ---------------------------------------------------


class _FrameStream:
    def __init__(self, data: bytes = b"") -> None:
        self.buf = io.BytesIO(data)

    def read(self, n: int) -> bytes:
        return self.buf.read(n)

    def write(self, data: bytes) -> None:
        self.buf.write(data)

    def flush(self) -> None:
        pass

    @property
    def data(self) -> bytes:
        return self.buf.getvalue()


class WebSocketCodecTest(unittest.TestCase):
    def test_accept_key_matches_rfc_vector(self) -> None:
        # RFC 6455 §4.2.2 reference vector.
        self.assertEqual(
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
            websocket_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
        )

    def test_send_binary_frame_format(self) -> None:
        stream = _FrameStream()
        ws = WebSocket(stream, stream)
        ws.send_binary(b"abc")
        # FIN(1) + binary(2) = 0x82; length 3; payload unmasked.
        self.assertEqual(b"\x82\x03abc", stream.data)

    def test_send_large_text_frame_uses_extended_length(self) -> None:
        stream = _FrameStream()
        ws = WebSocket(stream, stream)
        payload = "x" * 200
        ws.send_text(payload)
        data = stream.data
        self.assertEqual(0x81, data[0])  # FIN + text
        self.assertEqual(126, data[1])  # 16-bit length marker
        self.assertEqual(200, struct.unpack(">H", data[2:4])[0])
        self.assertEqual(payload.encode(), data[4:])

    def test_recv_decodes_masked_client_frame(self) -> None:
        payload = b"hello"
        mask = b"\x12\x34\x56\x78"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x82, 0x80 | len(payload)]) + mask + masked
        ws = WebSocket(_FrameStream(frame), _FrameStream())
        opcode, data = ws.recv()  # type: ignore[misc]
        self.assertEqual(OPCODE_BINARY, opcode)
        self.assertEqual(payload, data)

    def test_recv_handles_ping_with_pong(self) -> None:
        payload = b"ping"
        mask = b"\xaa\xbb\xcc\xdd"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        ping_frame = bytes([0x89, 0x80 | len(payload)]) + mask + masked
        text_frame = self._masked_text_frame("hi")
        stream = _FrameStream(ping_frame + text_frame)
        out = _FrameStream()
        ws = WebSocket(stream, out)
        opcode, data = ws.recv()  # type: ignore[misc]
        self.assertEqual(OPCODE_TEXT, opcode)
        self.assertEqual(b"hi", data)
        # A pong frame must have been written back for the ping.
        self.assertEqual(OPCODE_PONG, out.data[0] & 0x0F)

    def test_recv_returns_none_on_close(self) -> None:
        # close frame with masked 2-byte status payload (1000).
        mask = b"\x11\x22\x33\x44"
        status = bytes([0x03, 0xe8])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(status))
        close_frame = bytes([0x88, 0x82]) + mask + masked
        ws = WebSocket(_FrameStream(close_frame), _FrameStream())
        self.assertIsNone(ws.recv())

    @staticmethod
    def _masked_text_frame(text: str) -> bytes:
        payload = text.encode()
        mask = b"\x09\x08\x07\x06"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes([0x81, 0x80 | len(payload)]) + mask + masked


# -- WebSocket bridge integration -------------------------------------------


class _BridgeHandle:
    def __init__(self, on_output, on_exit) -> None:
        self.on_output = on_output
        self.on_exit = on_exit
        self.input = bytearray()
        self.resizes: list[tuple[int, int]] = []
        self.killed = False


class _BridgePtyBackend(FakePtyBackend):
    """Echoes input back as output so the WS round-trip can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.handles: dict[str, _BridgeHandle] = {}

    def start_pty(self, sandbox, pty_id, *, rows, cols, command, cwd, envs,
                  on_output, on_exit):
        handle = _BridgeHandle(on_output, on_exit)
        self.handles[pty_id] = handle
        with self._lock:
            self._counter += 1
            pid = self._counter
        self.calls.append(("start", pty_id, rows, cols, command))

        def ready() -> None:
            on_output(b"READY\n")

        threading.Thread(target=ready, daemon=True).start()
        return handle, pid

    def send_pty_input(self, sandbox, handle, data):
        handle.input.extend(data)
        handle.on_output(b"echo:" + data)

    def resize_pty(self, sandbox, handle, rows, cols):
        handle.resizes.append((rows, cols))

    def kill_pty(self, sandbox, handle):
        handle.killed = True
        handle.on_exit(0)


def _ws_handshake(sock: socket.socket, path: str, host: str, port: int) -> bytes:
    key = os.urandom(16).hex()
    import base64

    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    return response


def _ws_send(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
    sock.sendall(header + masked)


def _ws_recv(sock: socket.socket) -> tuple[int, bytes] | None:
    header = sock.recv(2)
    if len(header) < 2:
        return None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    payload = sock.recv(length) if length else b""
    return opcode, payload


class WebSocketBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _BridgePtyBackend()
        self.orchestrator = Orchestrator(tempfile.mkdtemp(), backend=self.backend)
        template = self.orchestrator.create_template("bridge", source="scratch")
        self.sandbox = self.orchestrator.create_sandbox(template.id)
        from agentenv.api import AgentEnvApi, make_handler

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(AgentEnvApi(self.orchestrator))
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path: str, body: dict) -> dict:
        data = __import__("json").dumps(body).encode()
        conn = http_conn(self.port)
        conn.request("POST", path, body=data, headers={"content-type": "application/json"})
        resp = conn.getresponse()
        payload = resp.read().decode()
        conn.close()
        self.assertEqual(201, resp.status, payload)
        return __import__("json").loads(payload)

    def test_create_handshake_round_trip_and_resize(self) -> None:
        info = self._post(f"/sandboxes/{self.sandbox.id}/pty", {})
        pty_id = info["ptyID"]

        sock = socket.create_connection(("127.0.0.1", self.port))
        try:
            response = _ws_handshake(
                sock, f"/sandboxes/{self.sandbox.id}/pty/{pty_id}?offset=0",
                "127.0.0.1", self.port,
            )
            self.assertIn(b"101", response.split(b"\r\n")[0])

            # Receive the READY banner (binary frame).
            deadline = time.monotonic() + 3
            received = b""
            while time.monotonic() < deadline:
                frame = _ws_recv(sock)
                if frame is None:
                    break
                opcode, payload = frame
                received += payload
                if b"READY" in received:
                    break
            self.assertIn(b"READY", received)

            # Send input (binary) and receive the echo.
            _ws_send(sock, OPCODE_BINARY, b"hi\n")
            echoed = b""
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                frame = _ws_recv(sock)
                if frame is None:
                    break
                echoed += frame[1]
                if b"echo:hi" in echoed:
                    break
            self.assertIn(b"echo:hi", echoed)

            # Send a resize control (text) and assert it reaches the backend.
            _ws_send(sock, OPCODE_TEXT, b'{"type":"resize","rows":40,"cols":120}')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.backend.handles[pty_id].resizes:
                    break
                time.sleep(0.05)
            self.assertEqual([(40, 120)], self.backend.handles[pty_id].resizes)
        finally:
            sock.close()


def http_conn(port: int):
    import http.client

    return http.client.HTTPConnection("127.0.0.1", port, timeout=5)


if __name__ == "__main__":
    unittest.main()