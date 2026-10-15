"""Workspace-scoped run records shared by Fluxen instances."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

ACTIVE = {"Running", "Waiting approval"}


class Runs:
    def __init__(self, session_dir: Path):
        self.directory = session_dir / "runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.current: dict | None = None

    def start(self, task: str, model: str):
        self.current = dict(
            id=uuid.uuid4().hex,
            pid=os.getpid(),
            task=task,
            model=model,
            status="Running",
            progress="Starting",
            started=time.time(),
            updated=time.time(),
        )
        self.save()

    def save(self):
        if self.current is None:
            return
        self.current["updated"] = time.time()
        path = self.directory / (self.current["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.current), encoding="utf-8")
        temporary.replace(path)

    def update(self, **changes):
        if self.current is not None:
            self.current.update(changes)
            self.save()

    def list(self):
        records = []
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or not all(
                    key in record for key in ("id", "task", "model", "status", "progress", "started", "updated")
                ):
                    continue
                if not all(isinstance(record[key], str) for key in ("id", "task", "model", "status", "progress")):
                    continue
                if not all(isinstance(record[key], (int, float)) for key in ("started", "updated")):
                    continue
                if record["status"] in ACTIVE and time.time() - record["updated"] > 15:
                    record["status"] = "Interrupted"
                records.append(record)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(records, key=lambda row: (row["status"] in ACTIVE, row["started"]), reverse=True)[:20]
