"""Backend -> UI event model.

The core package never imports Qt. Services report through an ``EventSink``
(any callable taking one event), so they can be tested with ``list.append`` and
adapted to Qt signals in ``app/ui/workers.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Union


class Level(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class State(str, Enum):
    STARTED = "started"  # server is listening / connection established
    STOPPED = "stopped"  # server stopped or job cancelled by the user
    FINISHED = "finished"  # one-shot job completed successfully
    FAILED = "failed"  # job could not start or aborted with an error


@dataclass(frozen=True)
class LogEvent:
    level: Level
    message: str
    ts: float = field(default_factory=time.time)
    tag: str = ""  # "transfer" = one transfer completed successfully (drives desktop notifications)


@dataclass(frozen=True)
class ProgressEvent:
    sent: int
    total: int
    bps: float
    peer: str = ""


@dataclass(frozen=True)
class StateEvent:
    state: State
    detail: str = ""
    transient: bool = False  # FAILED only: a retry might succeed (timeout, connection reset), unlike bad credentials


@dataclass(frozen=True)
class BatchEvent:
    """Per-target status in a batch push (status: queued/running/done/failed/cancelled)."""

    index: int
    host: str
    status: str
    detail: str = ""
    sent: int = 0
    total: int = 0


Event = Union[LogEvent, ProgressEvent, StateEvent, BatchEvent]
EventSink = Callable[[Event], None]
