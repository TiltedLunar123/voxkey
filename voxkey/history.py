"""Recent dictations, kept on disk so they survive a restart."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import HISTORY_PATH


@dataclass
class Entry:
    text: str
    raw: str = ""
    profile: str = ""
    seconds: float = 0.0
    at: float = field(default_factory=time.time)

    @property
    def when(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.at))

    @property
    def day(self) -> str:
        return time.strftime("%d %b", time.localtime(self.at))


class History:
    def __init__(self, config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self.entries: list[Entry] = []
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            self.entries = [Entry(**item) for item in raw][: self._cap()]
        except (OSError, ValueError, TypeError):
            self.entries = []

    def _cap(self) -> int:
        return max(1, int(self.config.get("history.max_items", 100)))

    def save(self) -> None:
        with self._lock:
            try:
                HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                payload = [asdict(entry) for entry in self.entries[: self._cap()]]
                HISTORY_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            except OSError:
                pass

    def add(self, entry: Entry) -> None:
        if not self.config.get("history.enabled", True) or not entry.text.strip():
            return
        with self._lock:
            self.entries.insert(0, entry)
            del self.entries[self._cap():]
        self.save()

    def clear(self) -> None:
        with self._lock:
            self.entries = []
        try:
            HISTORY_PATH.unlink(missing_ok=True)
        except OSError:
            pass
