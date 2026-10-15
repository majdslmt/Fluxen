"""Atomic local JSON session persistence, one directory per workspace."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_data_dir


class Sessions:
    def __init__(self, workspace: Path):
        key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
        self.dir = Path(user_data_dir("fluxen")) / "sessions" / key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.id = uuid.uuid4().hex[:12]
        self.messages: list[dict] = []

    def save(self):
        p = self.dir / f"{self.id}.json"
        tmp = p.with_suffix(".tmp")
        data = {"id": self.id, "updated": datetime.now(UTC).isoformat(), "messages": self.messages}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def _paths(self) -> list[Path]:
        return sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        data.setdefault("id", path.stem)
        return data

    def resume(self, session_id: str | None = None) -> bool:
        """Load the given session, or the most recent readable one. Returns False if none matched."""
        if session_id:
            matches = [p for p in self._paths() if p.stem.startswith(session_id)]
            if len(matches) != 1:
                return False
            candidates = matches
        else:
            candidates = self._paths()
        for path in candidates:
            data = self._read(path)
            if data is not None:
                self.id = str(data["id"])
                self.messages = data["messages"]
                return True
        return False

    def new(self):
        self.id = uuid.uuid4().hex[:12]
        self.messages = []

    def list(self, limit: int = 20) -> list[dict]:
        """Most recent sessions first, with a short preview of the first request."""
        result = []
        for path in self._paths()[:limit]:
            data = self._read(path)
            if data is None:
                continue
            first = next((m.get("content") for m in data["messages"] if m.get("role") == "user"), "")
            result.append(
                {
                    "id": str(data["id"]),
                    "updated": data.get("updated", ""),
                    "messages": len(data["messages"]),
                    "preview": str(first or "").replace("\n", " ")[:60],
                }
            )
        return result
