from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .e2b import (
    lifecycle_from_request,
    network_from_request,
    resources_from_request,
    sandbox_to_e2b,
)
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
        event_match = re.fullmatch(r"/events/sandboxes/([^/]+)", path)
        if method == "GET" and event_match:
            sandbox_id = event_match.group(1)
            limit = min(int(query.get("limit", ["10"])[0]), 100)
            offset = int(query.get("offset", ["0"])[0])
            order_ascending = query.get("orderAsc", ["false"])[0].lower() == "true"
            types = set(query.get("types", []))
            events = [
                event
                for event in self.orchestrator.list_events(limit=None)
                if (
                    event.resource_id == sandbox_id
                    or event.details.get("sandbox_id") == sandbox_id
                )
                and (not types or event.type in types)
            ]
            if order_ascending:
                events.reverse()
            return 200, [
                event.to_dict() for event in events[offset : offset + limit]
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
            return 200, [
                sandbox_to_e2b(item) for item in self._filtered_sandboxes(query)
            ]
        if method == "GET" and path == "/v2/sandboxes":
            return 200, [
                sandbox_to_e2b(item) for item in self._filtered_sandboxes(query)
            ]
        if method == "POST" and path == "/sandboxes":
            e2b_request = "templateID" in body
            timeout_action, auto_resume = lifecycle_from_request(body)
            item = self.orchestrator.create_sandbox(
                body.get("template_id", body.get("templateID")),
                snapshot_id=body.get("snapshot_id"),
                env=body.get("env", body.get("envVars")),
                timeout_seconds=self._timeout(
                    body, default=15 if e2b_request else None
                ),
                metadata=body.get("metadata"),
                resources=resources_from_request(body),
                network=network_from_request(body),
                timeout_action=timeout_action,
                auto_resume=auto_resume,
            )
            return 201, sandbox_to_e2b(item) if e2b_request else item.to_dict()
        if method == "POST" and path == "/sandboxes-cold":
            timeout_action, auto_resume = lifecycle_from_request(body)
            item = self.orchestrator.create_cold_sandbox(
                body["image"],
                timeout_seconds=self._timeout(body, default=15),
                env=body.get("env", body.get("envVars")),
                metadata=body.get("metadata"),
                resources=resources_from_request(body),
                network=network_from_request(body),
                timeout_action=timeout_action,
                auto_resume=auto_resume,
            )
            return 201, sandbox_to_e2b(item)
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
            return 200, sandbox_to_e2b(
                self.orchestrator.get_sandbox(sandbox_id)
            )
        if method == "DELETE" and action is None:
            self.orchestrator.delete(sandbox_id)
            return 204, {}
        if method == "POST" and action == "exec":
            result = self.orchestrator.execute(
                sandbox_id, body["command"], body.get("timeout")
            )
            return 200, result.to_dict()
        if method == "POST" and action == "pause":
            self.orchestrator.pause(sandbox_id)
            return 204, {}
        if method == "POST" and action == "resume":
            item = self.orchestrator.resume(sandbox_id)
            timeout = self._timeout(body)
            if timeout is not None:
                item = self.orchestrator.update_timeout(sandbox_id, timeout)
            return 201, sandbox_to_e2b(item)
        if method == "POST" and action == "connect":
            item = self.orchestrator.get_sandbox(sandbox_id)
            was_paused = item.state.value == "paused"
            if was_paused:
                item = self.orchestrator.resume(sandbox_id)
            timeout = self._timeout(body)
            if timeout is not None:
                item = self.orchestrator.update_timeout(sandbox_id, timeout)
            return 201 if was_paused else 200, sandbox_to_e2b(item)
        if method == "POST" and action == "snapshots":
            return 201, self.orchestrator.snapshot(sandbox_id).to_dict()
        if method == "POST" and action == "fork":
            children = self.orchestrator.fork(sandbox_id, body.get("count", 1))
            return 201, [
                {"sandbox": sandbox_to_e2b(item), "error": None}
                for item in children
            ]
        if method == "POST" and action == "timeout":
            self.orchestrator.update_timeout(sandbox_id, self._timeout(body))
            return 204, {}
        if method == "PUT" and action == "network":
            item = self.orchestrator.update_network(
                sandbox_id, network_from_request(body) or body
            )
            return 200, sandbox_to_e2b(item)
        if method == "PUT" and action == "resources":
            resources = resources_from_request(body)
            if resources is None:
                raise AgentEnvError("resource update cannot be empty")
            item = self.orchestrator.update_resources(sandbox_id, resources)
            return 200, sandbox_to_e2b(item)
        return 404, {"error": "route not found"}

    def _timeout(
        self, body: dict[str, Any], default: int | None = None
    ) -> int | None:
        value = body.get("timeout_seconds", body.get("timeout", default))
        return None if value in (None, 0) else int(value)

    def _filtered_sandboxes(self, query: dict[str, list[str]]):
        sandboxes = self.orchestrator.list_sandboxes()
        states = {
            state
            for value in query.get("state", [])
            for state in value.split(",")
            if state
        }
        if states:
            sandboxes = [
                sandbox
                for sandbox in sandboxes
                if (
                    "paused"
                    if sandbox.state.value == "paused"
                    else "running"
                )
                in states
            ]
        metadata_values = query.get("metadata", [])
        if metadata_values:
            metadata = {
                key: values[-1]
                for key, values in parse_qs(metadata_values[-1]).items()
            }
            sandboxes = [
                sandbox
                for sandbox in sandboxes
                if all(sandbox.metadata.get(key) == value for key, value in metadata.items())
            ]
        limit = int(query.get("limit", [str(len(sandboxes) or 100)])[0])
        return sandboxes[:limit]


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
            encoded = (
                b""
                if int(status) == HTTPStatus.NO_CONTENT
                else json.dumps(result, ensure_ascii=False).encode()
            )
            self.send_response(int(status))
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle
        do_PUT = _handle

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
