"""The only place where the Qt-free core meets Qt.

``JobRunner.emit_event`` is the EventSink handed to a service. The service
calls it from a worker thread; it re-emits the event as a Qt Signal. Because the
runner lives in the GUI thread, Qt delivers those signals to widget slots as
queued calls on the GUI thread - widgets are never touched from a worker.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..core.events import BatchEvent, Event, Level, LogEvent, ProgressEvent, State, StateEvent


class _JobThread(QThread):
    def __init__(self, job: Callable[[], None], on_error: Callable[[Event], None], parent: QObject) -> None:
        super().__init__(parent)
        self._job = job
        self._on_error = on_error

    def run(self) -> None:
        try:
            self._job()
        except Exception as exc:
            self._on_error(LogEvent(Level.ERROR, f"Unexpected error: {exc!r}"))
            self._on_error(StateEvent(State.FAILED, str(exc)))


class JobRunner(QObject):
    """Runs one blocking service call at a time on a QThread."""

    log = Signal(object)  # LogEvent
    progress = Signal(object)  # ProgressEvent
    state = Signal(object)  # StateEvent
    batch = Signal(object)  # BatchEvent
    idle = Signal()  # worker thread has fully finished

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[_JobThread] = None

    # EventSink for services (called from the worker thread)
    def emit_event(self, event: Event) -> None:
        if isinstance(event, LogEvent):
            self.log.emit(event)
        elif isinstance(event, ProgressEvent):
            self.progress.emit(event)
        elif isinstance(event, StateEvent):
            self.state.emit(event)
        elif isinstance(event, BatchEvent):
            self.batch.emit(event)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, job: Callable[[], None]) -> None:
        if self._thread is not None:
            return
        thread = _JobThread(job, self.emit_event, self)
        thread.finished.connect(self._on_finished)
        self._thread = thread
        thread.start()

    def wait(self, msecs: int = 8000) -> bool:
        thread = self._thread
        return thread.wait(msecs) if thread is not None else True

    def _on_finished(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        self.idle.emit()
