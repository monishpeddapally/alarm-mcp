"""Data models for alarms."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

AlarmStatus = Literal["pending", "armed", "triggered", "cancelled", "error"]


class Alarm(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    condition: str  # natural language: "Rishabh Pant comes to bat"
    label: str = ""  # human label shown when fired
    source_hint: Optional[str] = None  # "cricket", "news", "price:BTC", etc.
    poll_seconds: int = 30
    status: AlarmStatus = "pending"
    created_at: float = Field(default_factory=time.time)
    triggered_at: Optional[float] = None
    last_checked_at: Optional[float] = None
    last_check_summary: Optional[str] = None
    last_evidence: Optional[str] = None  # snippet of data that satisfied condition
    error: Optional[str] = None
    check_count: int = 0


class Store:
    """Tiny JSON-on-disk store. Single-process; we hold a lock at the server level."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._alarms: dict[str, Alarm] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            self._alarms = {aid: Alarm.model_validate(a) for aid, a in raw.items()}
        except Exception:
            # corrupt file: keep going with empty store, don't crash
            self._alarms = {}

    def save(self) -> None:
        data = {aid: a.model_dump() for aid, a in self._alarms.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def all(self) -> list[Alarm]:
        return list(self._alarms.values())

    def get(self, alarm_id: str) -> Optional[Alarm]:
        return self._alarms.get(alarm_id)

    def put(self, alarm: Alarm) -> None:
        self._alarms[alarm.id] = alarm
        self.save()

    def delete(self, alarm_id: str) -> bool:
        if alarm_id in self._alarms:
            del self._alarms[alarm_id]
            self.save()
            return True
        return False

    def active(self) -> list[Alarm]:
        return [a for a in self._alarms.values() if a.status in ("pending", "armed")]
