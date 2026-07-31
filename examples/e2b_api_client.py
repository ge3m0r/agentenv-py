#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class AgentEnvApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        encoded = (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            method=method,
            headers={"content-type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read()
                return response.status, json.loads(content) if content else None
        except HTTPError as error:
            content = error.read()
            details = json.loads(content) if content else {"error": str(error)}
            raise RuntimeError(
                f"{method} {path} failed with HTTP {error.code}: {details}"
            ) from error

    def ensure_template(self, name: str, source: str) -> None:
        try:
            self.request(
                "POST",
                "/templates",
                {"name": name, "source": source},
            )
        except RuntimeError as error:
            if "HTTP 409" not in str(error):
                raise

    def create_from_template(self, template: str) -> dict[str, Any]:
        _, sandbox = self.request(
            "POST",
            "/sandboxes",
            {
                "templateID": template,
                "timeout": 300,
                "envVars": {"MESSAGE": "hello-from-e2b-api"},
                "metadata": {"example": "python-client"},
                "cpuCount": 1,
                "memoryMB": 256,
                "lifecycle": {
                    "onTimeout": "pause",
                    "autoResume": True,
                },
            },
        )
        return sandbox

    def create_from_image(self, image: str) -> dict[str, Any]:
        _, sandbox = self.request(
            "POST",
            "/sandboxes-cold",
            {
                "image": image,
                "timeout": 300,
                "envVars": {"MESSAGE": "hello-from-e2b-api"},
                "cpuCount": 1,
                "memoryMB": 256,
            },
        )
        return sandbox

    def execute(self, sandbox_id: str, command: str) -> dict[str, Any]:
        _, result = self.request(
            "POST",
            f"/sandboxes/{sandbox_id}/exec",
            {"command": command, "timeout": 30},
        )
        return result

    def pause(self, sandbox_id: str) -> None:
        self.request("POST", f"/sandboxes/{sandbox_id}/pause")

    def connect(self, sandbox_id: str) -> dict[str, Any]:
        _, sandbox = self.request(
            "POST",
            f"/sandboxes/{sandbox_id}/connect",
            {"timeout": 600},
        )
        return sandbox

    def events(self, sandbox_id: str) -> list[dict[str, Any]]:
        _, events = self.request(
            "GET", f"/events/sandboxes/{sandbox_id}?limit=10"
        )
        return events

    def kill(self, sandbox_id: str) -> None:
        self.request("DELETE", f"/sandboxes/{sandbox_id}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call the AgentENV E2B-compatible HTTP API"
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8000"
    )
    parser.add_argument("--template", default="e2b-api-demo")
    parser.add_argument("--source", default="scratch")
    parser.add_argument(
        "--cold-image",
        help="Use /sandboxes-cold instead of creating a template",
    )
    args = parser.parse_args()

    client = AgentEnvApiClient(args.base_url)
    if args.cold_image:
        sandbox = client.create_from_image(args.cold_image)
    else:
        client.ensure_template(args.template, args.source)
        sandbox = client.create_from_template(args.template)

    sandbox_id = sandbox["sandboxID"]
    print("created:", json.dumps(sandbox, ensure_ascii=False, indent=2))
    try:
        result = client.execute(
            sandbox_id,
            'printf "%s\\n" "$MESSAGE" > result.txt && cat result.txt',
        )
        print("command:", json.dumps(result, ensure_ascii=False, indent=2))

        client.pause(sandbox_id)
        print("paused:", sandbox_id)

        connected = client.connect(sandbox_id)
        print(
            "connected:",
            json.dumps(connected, ensure_ascii=False, indent=2),
        )

        events = client.events(sandbox_id)
        print("events:", json.dumps(events, ensure_ascii=False, indent=2))
    finally:
        client.kill(sandbox_id)
        print("deleted:", sandbox_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
