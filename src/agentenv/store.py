from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from .models import LifecycleEvent, Sandbox, Snapshot, Template


class JsonMetadataStore:
    """Tiny durable metadata store standing in for AgentENV's store/persister."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "metadata.json"
        self._lock = RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(
                {"templates": {}, "sandboxes": {}, "snapshots": {}, "events": {}}
            )

    def _read(self) -> dict[str, Any]:
        with self.path.open(encoding="utf-8") as file:
            data = json.load(file)
        for key in ("templates", "sandboxes", "snapshots", "events"):
            data.setdefault(key, {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)

    def _all(self, key: str, model: type) -> list[Any]:
        with self._lock:
            return [model.from_dict(item) for item in self._read()[key].values()]

    def _get(self, key: str, item_id: str, model: type) -> Any | None:
        with self._lock:
            item = self._read()[key].get(item_id)
            return model.from_dict(item) if item else None

    def _put(self, key: str, item: Any) -> None:
        with self._lock:
            data = self._read()
            data[key][item.id] = item.to_dict()
            self._write(data)

    def _delete(self, key: str, item_id: str) -> None:
        with self._lock:
            data = self._read()
            data[key].pop(item_id, None)
            self._write(data)

    def list_templates(self) -> list[Template]:
        return self._all("templates", Template)

    def get_template(self, template_id: str) -> Template | None:
        templates = self.list_templates()
        return next(
            (item for item in templates if item.id == template_id or item.name == template_id),
            None,
        )

    def put_template(self, template: Template) -> None:
        self._put("templates", template)

    def delete_template(self, template_id: str) -> None:
        self._delete("templates", template_id)

    def list_sandboxes(self) -> list[Sandbox]:
        return self._all("sandboxes", Sandbox)

    def get_sandbox(self, sandbox_id: str) -> Sandbox | None:
        return self._get("sandboxes", sandbox_id, Sandbox)

    def put_sandbox(self, sandbox: Sandbox) -> None:
        self._put("sandboxes", sandbox)

    def delete_sandbox(self, sandbox_id: str) -> None:
        self._delete("sandboxes", sandbox_id)

    def list_snapshots(self) -> list[Snapshot]:
        return self._all("snapshots", Snapshot)

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        return self._get("snapshots", snapshot_id, Snapshot)

    def put_snapshot(self, snapshot: Snapshot) -> None:
        self._put("snapshots", snapshot)

    def delete_snapshot(self, snapshot_id: str) -> None:
        self._delete("snapshots", snapshot_id)

    def list_events(self, limit: int | None = None) -> list[LifecycleEvent]:
        events = self._all("events", LifecycleEvent)
        events.sort(key=lambda event: event.occurred_at, reverse=True)
        return events[:limit] if limit is not None else events

    def put_event(self, event: LifecycleEvent) -> None:
        self._put("events", event)
