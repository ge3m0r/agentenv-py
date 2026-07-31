"""Minimal RFC 6455 WebSocket server, implemented with the standard library.

The project keeps a zero-runtime-dependency policy, so the PTY transport is a
hand-written WebSocket layer bolted onto the existing ``ThreadingHTTPServer``:
no ``websockets``/``aiohttp`` needed. It supports the subset real terminal
clients use — text/binary frames, ping/pong, close, and message fragmentation.

Wire protocol (client→server frames are masked; server→client are not):

* binary frames  = raw PTY input / output bytes
* text frames     = JSON control messages (``{"type":"resize",...}``)
* close frame     = tear down the session transport
"""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any, Optional

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


def websocket_accept_key(key: str) -> str:
    """RFC 6455 §1.3: the server accept value derived from the client key."""
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def websocket_handshake(handler: Any) -> bool:
    """Upgrade ``handler`` to a WebSocket. Returns False (and sends 400) if the
    request is not a valid handshake."""
    key = handler.headers.get("sec-websocket-key")
    version = handler.headers.get("sec-websocket-version", "13")
    if not key or version != "13":
        handler.send_response(400)
        handler.send_header("content-type", "text/plain")
        handler.end_headers()
        handler.wfile.write(b"invalid websocket handshake")
        return False
    handler.send_response(101)
    handler.send_header("upgrade", "websocket")
    handler.send_header("connection", "Upgrade")
    handler.send_header("sec-websocket-accept", websocket_accept_key(key))
    handler.end_headers()
    return True


class WebSocket:
    """A framed WebSocket connection over ``rfile``/``wfile``.

    ``recv`` returns ``(opcode, payload)`` for a complete text/binary message
    (handling fragmentation), or ``None`` when the peer closes the connection.
    Control frames (ping/pong) are handled inline.
    """

    def __init__(self, rfile: Any, wfile: Any) -> None:
        self.rfile = rfile
        self.wfile = wfile
        self.closed = False

    def recv(self) -> Optional[tuple[int, bytes]]:
        message = bytearray()
        message_opcode: Optional[int] = None
        while True:
            header = self.rfile.read(2)
            if len(header) < 2:
                return None
            b1, b2 = header[0], header[1]
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                ext = self.rfile.read(2)
                if len(ext) < 2:
                    return None
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = self.rfile.read(8)
                if len(ext) < 8:
                    return None
                length = struct.unpack(">Q", ext)[0]
            mask = self.rfile.read(4) if masked else b""
            payload = self.rfile.read(length) if length else b""
            if len(payload) < length:
                return None
            if masked:
                payload = bytes(
                    b ^ mask[i % 4] for i, b in enumerate(payload)
                )

            if opcode == OPCODE_CLOSE:
                self.close()
                return None
            if opcode == OPCODE_PING:
                self._send(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue

            if opcode == OPCODE_CONT:
                if message_opcode is None:
                    return None  # continuation without a start frame
                message.extend(payload)
            else:
                if message_opcode is not None and message:
                    # peer started a new message mid-fragmentation; reset
                    message = bytearray()
                message.extend(payload)
                message_opcode = opcode

            if fin:
                assert message_opcode is not None
                return message_opcode, bytes(message)

    def send_binary(self, data: bytes) -> None:
        self._send(OPCODE_BINARY, data)

    def send_text(self, text: str) -> None:
        self._send(OPCODE_TEXT, text.encode("utf-8"))

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send(OPCODE_CLOSE, struct.pack(">H", code))
        except OSError:
            pass

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed and opcode != OPCODE_CLOSE:
            return
        header = bytearray([0x80 | opcode])  # FIN set
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n <= 0xFFFF:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        self.wfile.write(bytes(header) + payload)
        self.wfile.flush()
