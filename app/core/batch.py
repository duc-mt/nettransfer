"""Batch push: many devices in parallel, per-device status, retry-only-failures."""

from __future__ import annotations

import csv
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .events import BatchEvent, Event, EventSink, Level, LogEvent, ProgressEvent, State, StateEvent

CSV_COLUMNS = ("host", "username", "password", "remote_path", "port")


@dataclass(frozen=True)
class BatchTarget:
    index: int
    host: str
    username: str = ""
    password: str = ""
    remote_path: str = ""
    port: Optional[int] = None


class BatchFileError(Exception):
    def __init__(self, errors: List[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def load_targets_csv(path: str) -> List[BatchTarget]:
    """Header row required; only ``host`` is mandatory. Blank username/password/port fall back to the
    shared defaults. Any bad row aborts the load: silently skipping a device in a firmware rollout is worse."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header: Optional[List[str]] = None
        targets: List[BatchTarget] = []
        errors: List[str] = []
        for row in reader:
            if not row or all(not c.strip() for c in row) or row[0].lstrip().startswith("#"):
                continue
            if header is None:
                header = [c.strip().lower() for c in row]
                if "host" not in header:
                    raise BatchFileError(
                        [f"header must contain a 'host' column (known columns: {', '.join(CSV_COLUMNS)})"]
                    )
                unknown = [c for c in header if c not in CSV_COLUMNS]
                if unknown:
                    raise BatchFileError([f"unknown column(s): {', '.join(unknown)}"])
                continue
            line = reader.line_num
            cells = dict(zip(header, (c.strip() for c in row)))
            host = cells.get("host", "")
            if not host:
                errors.append(f"line {line}: host is empty")
                continue
            port: Optional[int] = None
            if cells.get("port"):
                try:
                    port = int(cells["port"])
                    if not 1 <= port <= 65535:
                        raise ValueError
                except ValueError:
                    errors.append(f"line {line}: invalid port '{cells['port']}'")
                    continue
            targets.append(
                BatchTarget(
                    len(targets),
                    host,
                    cells.get("username", ""),
                    cells.get("password", ""),
                    cells.get("remote_path", ""),
                    port,
                )
            )
    if header is None:
        raise BatchFileError(["the file is empty"])
    if errors:
        raise BatchFileError(errors)
    if not targets:
        raise BatchFileError(["no device rows found"])
    return targets


def write_results_csv(path: str, targets: Sequence[BatchTarget], results: Dict[int, Tuple[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["host", "status", "detail"])
        for t in targets:
            status, detail = results.get(t.index, ("not run", ""))
            writer.writerow([t.host, status, detail])


class BatchRunner:
    """``service_factory(sink)`` -> object with push(req)/cancel(); ``request_factory(target)`` -> request.
    Both are injected, so the runner works for any protocol."""

    def __init__(
        self,
        on_event: EventSink,
        service_factory: Callable[[EventSink], object],
        request_factory: Callable[[BatchTarget], object],
        max_workers: int = 4,
    ) -> None:
        self._emit = on_event
        self._service_factory, self._request_factory = service_factory, request_factory
        self._workers = max(1, max_workers)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._active: Dict[int, object] = {}
        self.results: Dict[int, Tuple[str, str]] = {}

    def run(self, targets: Sequence[BatchTarget]) -> None:
        self._cancel.clear()
        self.results = {}
        self._emit(StateEvent(State.STARTED, f"{len(targets)} devices"))
        for t in targets:
            self._emit(BatchEvent(t.index, t.host, "queued"))
        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="batch") as pool:
            list(pool.map(self._run_one, targets))
        counts = {s: sum(1 for st, _ in self.results.values() if st == s) for s in ("done", "failed", "cancelled")}
        summary = f"Batch finished: {counts['done']} ok, {counts['failed']} failed, {counts['cancelled']} cancelled"
        self._emit(
            LogEvent(
                Level.SUCCESS if not counts["failed"] and not counts["cancelled"] else Level.WARNING,
                summary,
                tag="batch",
            )
        )
        self._emit(StateEvent(State.FINISHED, summary))

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            services = list(self._active.values())
        for service in services:
            service.cancel()

    # -- internals ---------------------------------------------------------
    def _run_one(self, target: BatchTarget) -> None:
        if self._cancel.is_set():
            self._finish(target, "cancelled", "cancelled before start")
            return
        sink = self._make_sink(target)
        service = self._service_factory(sink)
        with self._lock:
            self._active[target.index] = service
        try:
            service.push(self._request_factory(target))
        except Exception as exc:
            sink(StateEvent(State.FAILED, f"{type(exc).__name__}: {exc}"))
        finally:
            with self._lock:
                self._active.pop(target.index, None)
            if target.index not in self.results:
                self._finish(target, "failed", "no result reported")

    def _make_sink(self, target: BatchTarget) -> Callable[[Event], None]:
        def sink(event: Event) -> None:
            if isinstance(event, LogEvent):
                self._emit(LogEvent(event.level, f"[{target.host}] {event.message}", event.ts, event.tag))
            elif isinstance(event, ProgressEvent):
                self._emit(BatchEvent(target.index, target.host, "running", "", event.sent, event.total))
            elif isinstance(event, StateEvent):
                if event.state is State.STARTED:
                    self._emit(BatchEvent(target.index, target.host, "running"))
                elif event.state is State.FINISHED:
                    self._finish(target, "done", "")
                elif event.state is State.FAILED:
                    self._finish(target, "failed", event.detail)
                elif event.state is State.STOPPED:
                    self._finish(target, "cancelled", event.detail)

        return sink

    def _finish(self, target: BatchTarget, status: str, detail: str) -> None:
        self.results[target.index] = (status, detail)
        self._emit(BatchEvent(target.index, target.host, status, detail))
