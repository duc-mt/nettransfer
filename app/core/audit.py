"""Append-only audit trail (one file per day) for change tickets. Never receives secrets."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .events import LogEvent


class AuditLog:
    def __init__(
        self,
        directory: Optional[Path],
        fixup: Optional[Callable[[Path], None]] = None,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir, self._fixup, self._clock = directory, fixup, clock
        self.enabled = enabled
        self._lock = threading.Lock()

    @property
    def path(self) -> Optional[Path]:
        if self._dir is None:
            return None
        return self._dir / f"nettransfer-{time.strftime('%Y%m%d', time.localtime(self._clock()))}.log"

    def write(self, source: str, event: LogEvent) -> None:
        path = self.path
        if not self.enabled or path is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts))
        message = " ".join(event.message.splitlines())
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                new = not path.exists()
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{stamp} [{source}] {event.level.value.upper():<7} {message}\n")
                if new and self._fixup:
                    self._fixup(path)
            except OSError:
                pass
