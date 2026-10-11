"""File digests, so an operator can compare against the device's own verify output."""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Dict, Optional, Sequence

from .events import EventSink, Level, LogEvent
from .formatting import human_size


def file_digests(
    path: str, algos: Sequence[str] = ("md5", "sha256"), chunk: int = 1 << 20, cancel: Optional[threading.Event] = None
) -> Dict[str, str]:
    hashers = {a: hashlib.new(a, usedforsecurity=False) for a in algos}
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("hashing cancelled")
            block = fh.read(chunk)
            if not block:
                break
            for h in hashers.values():
                h.update(block)
    return {a: h.hexdigest() for a, h in hashers.items()}


class HashService:
    def __init__(self, on_event: EventSink) -> None:
        self._emit = on_event
        self._cancel = threading.Event()

    def run(self, path: str) -> None:
        if not os.path.isfile(path):
            self._emit(LogEvent(Level.ERROR, f"File not found: {path}"))
            return
        name = os.path.basename(path)
        self._emit(LogEvent(Level.INFO, f"Hashing {name} ({human_size(os.path.getsize(path))}) ..."))
        try:
            digests = file_digests(path, cancel=self._cancel)
        except InterruptedError:
            self._emit(LogEvent(Level.WARNING, "Hashing cancelled"))
            return
        except OSError as exc:
            self._emit(LogEvent(Level.ERROR, f"Cannot read {name}: {exc}"))
            return
        for algo, digest in digests.items():
            self._emit(LogEvent(Level.SUCCESS, f"{algo.upper():<7}{digest}  {name}"))

    def cancel(self) -> None:
        self._cancel.set()
