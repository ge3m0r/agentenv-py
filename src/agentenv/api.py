from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .orchestrator import AgentEnvError, ConflictError, NotFoundError, Orchestrator


class AgentEnvApi:
    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator

    def dispatch(
        self, method: str, path: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | list[dict[str, Any]]]:
        parsed = urlsplit(path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/health":
            return 200, {"status": "ok", **self.orchestrator.status()}
        if method == "GET" and path == "/status":
            return 200, self.orchestrator.status()
        if method == "GET" and path == "/events":
            limit = int(query.get("limit", ["100"])[0])
            return 200, [
                item.to_dict() for item in self.orchestrator.list_events(limit)
            ]
        if method == "POST" and path == "/maintenance/evict":
            return 200, {"evicted": self.orchestrator.evict_expired()}
        if method == "GET" and path == "/templates":
            return 200, [item.to_dict() for item in self.orchestrator.list_templates()]
        if method == "POST" and path == "/templates":
            item = self.orchestrator.create_template(
                name=body["name"],
                source=body.get("source", "scratch"),
                base_dir=body.get("base_dir"),
                env=body.get("env"),
                workdir=body.get("workdir", "."),
            )
            return 201, item.to_dict()
        if method == "GET" and path == "/sandboxes":
            return 200, [item.to_dict() for item in self.orchestrator.list_sandboxes()]
        if method == "POST" and path == "/sandboxes":
            item = self.orchestrator.create_sandbox(
                body.get("template_id"),
                snapshot_id=body.get("snapshot_id"),
                env=body.get("env"),
                timeout_seconds=body.get("timeout_seconds"),
                metadata=body.get("metadata"),
            )
            return 201, item.to_dict()
        if method == "GET" and path == "/snapshots":
            return 200, [item.to_dict() for item in self.orchestrator.list_snapshots()]

        template_match = re.fullmatch(r"/templates/([^/]+)", path)
        if template_match and method == "DELETE":
            template_id = template_match.group(1)
            self.orchestrator.delete_template(template_id)
            return 200, {"deleted": template_id}

        snapshot_match = re.fullmatch(r"/snapshots/([^/]+)", path)
        if snapshot_match:
            snapshot_id = snapshot_match.group(1)
            if method == "GET":
                return 200, self.orchestrator.get_snapshot(snapshot_id).to_dict()
            if method == "DELETE":
                self.orchestrator.delete_snapshot(snapshot_id)
                return 200, {"deleted": snapshot_id}

        match = re.fullmatch(r"/sandboxes/([^/]+)(?:/(.+))?", path)
        if not match:
            return 404, {"error": "route not found"}
        sandbox_id, action = match.groups()
        if method == "GET" and action is None:
            return 200, self.orchestrator.get_sandbox(sandbox_id).to_dict()
        if method == "DELETE" and action is None:
            self.orchestrator.delete(sandbox_id)
            return 200, {"deleted": sandbox_id}
        if method == "POST" and action == "exec":
            result = self.orchestrator.execute(
                sandbox_id, body["command"], body.get("timeout")
            )
            return 200, result.to_dict()
        if method == "POST" and action == "pause":
            return 200, self.orchestrator.pause(sandbox_id).to_dict()
        if method == "POST" and action == "resume":
            return 200, self.orchestrator.resume(sandbox_id).to_dict()
        if method == "POST" and action == "snapshots":
            return 201, self.orchestrator.snapshot(sandbox_id).to_dict()
        if method == "POST" and action == "fork":
            children = self.orchestrator.fork(sandbox_id, body.get("count", 1))
            return 201, [item.to_dict() for item in children]
        if method == "POST" and action == "timeout":
            return 200, self.orchestrator.update_timeout(
                sandbox_id, body.get("timeout_seconds")
            ).to_dict()
        return 404, {"error": "route not found"}


def make_handler(api: AgentEnvApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            try:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length)) if length else {}
                status, result = api.dispatch(self.command, self.path, body)
            except NotFoundError as error:
                status, result = HTTPStatus.NOT_FOUND, {"error": str(error)}
            except ConflictError as error:
                status, result = HTTPStatus.CONFLICT, {"error": str(error)}
            except (AgentEnvError, KeyError, ValueError) as error:
                status, result = HTTPStatus.BAD_REQUEST, {"error": str(error)}
            except Exception as error:
                status, result = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)}
            encoded = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(int(status))
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[api] {format % args}")

    return Handler


class MaintenanceWorker:
    def __init__(self, orchestrator: Orchestrator, interval: float = 1.0):
        self.orchestrator = orchestrator
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="agentenv-maintenance", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval * 2))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.orchestrator.evict_expired()
            except Exception as error:
                print(f"[maintenance] eviction failed: {error}")


def serve(
    orchestrator: Orchestrator,
    host: str = "127.0.0.1",
    port: int = 8000,
    maintenance_interval: float = 1.0,
) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(AgentEnvApi(orchestrator)))
    maintenance = MaintenanceWorker(orchestrator, maintenance_interval)
    maintenance.start()
    print(f"AgentENV Python API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        maintenance.stop()
        server.server_close()
