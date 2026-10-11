"""Shared UI plumbing: the context handed to every tab, and thread-safe event wiring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, Slot

from ..core.audit import AuditLog
from ..core.events import LogEvent
from ..core.settings import SettingsStore
from .widgets import TransferPanel
from .workers import JobRunner


@dataclass
class UiContext:
    settings: SettingsStore
    audit: AuditLog
    notify: Callable[[str, str], None] = field(default=lambda title, message: None)


class _LogRouter(QObject):
    """Real QObject slots (not lambdas) so Qt delivers worker-thread signals on the GUI thread."""

    def __init__(self, ctx: UiContext, source: str, parent: QObject) -> None:
        super().__init__(parent)
        self._ctx, self._source = ctx, source

    @Slot(object)
    def on_log(self, event: LogEvent) -> None:
        self._ctx.audit.write(self._source, event)
        if event.tag == "transfer":
            self._ctx.notify(f"{self._source}: transfer complete", event.message)
        elif event.tag == "batch":
            self._ctx.notify(f"{self._source}: batch finished", event.message)


def wire_runner(runner: JobRunner, panel: TransferPanel, ctx: UiContext, source: str) -> None:
    runner.log.connect(panel.on_log)
    runner.progress.connect(panel.on_progress)
    router = _LogRouter(ctx, source, panel)
    runner.log.connect(router.on_log)
