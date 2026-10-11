"""Progress/speed tracking shared by both transfer services."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .events import EventSink, ProgressEvent


class ProgressTracker:
    """Turns byte counts into throttled ``ProgressEvent``s.

    * speed is measured over a sliding ``window`` (seconds), not the whole run
    * events are rate-limited to one per ``min_interval`` so a fast link cannot
      flood the GUI thread; the final event (sent >= total) is always emitted
    * safe to call from several threads
    """

    def __init__(
        self,
        total: int,
        sink: EventSink,
        peer: str = "",
        min_interval: float = 0.1,
        window: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._total = total
        self._sink = sink
        self._peer = peer
        self._min_interval = min_interval
        self._window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()
        self._samples: deque[tuple[float, int]] = deque([(self._started, 0)])
        self._sent = 0
        self._last_emit = float("-inf")

    def update(self, sent: int) -> None:
        """Report the absolute number of bytes transferred so far."""
        now = self._clock()
        with self._lock:
            self._sent = sent
            self._samples.append((now, sent))
            while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
                self._samples.popleft()
            if now - self._last_emit < self._min_interval and sent < self._total:
                return
            self._last_emit = now
            event = ProgressEvent(sent, self._total, self._speed(), self._peer)
        self._sink(event)

    def finish(self) -> None:
        """Force a final event at 100%."""
        with self._lock:
            self._sent = max(self._sent, self._total)
            self._last_emit = self._clock()
            event = ProgressEvent(self._sent, self._total, self._speed(), self._peer)
        self._sink(event)

    @property
    def average_bps(self) -> float:
        elapsed = self._clock() - self._started
        return self._sent / elapsed if elapsed > 0 else 0.0

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    def _speed(self) -> float:
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0
