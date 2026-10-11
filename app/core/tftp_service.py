"""TFTP server service (tftpy wrapper). Qt-free: reports through an EventSink.

How it stays zero-config: tftpy only calls ``dyn_file_func`` for names that are
not present in its root directory, so the root is an empty temp dir and *every*
read request is answered with the file the user selected, whatever name the
device asked for. Write requests are refused (read-only server).
"""

from __future__ import annotations

import errno
import io
import logging
import os
import tempfile
import threading
from typing import Optional

import tftpy

from .events import EventSink, Level, LogEvent, State, StateEvent
from .formatting import human_size, human_speed
from .progress import ProgressTracker
from .serve_policy import ServeGuard, ServePolicy

DEFAULT_PORT = 69
# tftpy uses this both as its select() timeout and as its retransmit timeout,
# so it also bounds how long stop() takes to take effect.
SELECT_TIMEOUT = 3


class _ProgressReader(io.BufferedReader):
    """Read-only file handed to tftpy; reports progress and the final outcome."""

    def __init__(self, path: str, peer: str, sink: EventSink, guard: ServeGuard) -> None:
        super().__init__(io.FileIO(path, "rb"))
        self._total = os.path.getsize(path)
        self._peer = peer
        self._sink = sink
        self._guard = guard
        self._sent = 0
        self._reported = False
        self._first_read = True
        self._tracker = ProgressTracker(self._total, sink, peer)

    def read(self, size: Optional[int] = -1) -> bytes:
        if self._first_read and size and size > 0:
            # tftpy reads exactly one negotiated block per call, so the first size is the block size
            self._first_read = False
            self._sink(LogEvent(Level.INFO, f"{self._peer}: block size {size} bytes"))
            if size <= 512:
                self._sink(
                    LogEvent(
                        Level.INFO,
                        f"{self._peer}: 512-byte blocks make TFTP slow - many devices can "
                        "negotiate more (e.g. Cisco IOS: 'ip tftp blocksize 8192')",
                    )
                )
        data = super().read(size)
        if data:
            self._sent += len(data)
            self._tracker.update(self._sent)
        return data

    def close(self) -> None:
        if not self.closed and not self._reported:
            self._reported = True
            self._report()
        super().close()

    def _report(self) -> None:
        success = self._sent >= self._total
        try:
            self._log_outcome(success)
        finally:
            self._guard.transfer_finished(success)

    def _log_outcome(self, success: bool) -> None:
        if success:
            self._tracker.finish()
            self._sink(
                LogEvent(
                    Level.SUCCESS,
                    f"{self._peer}: sent {human_size(self._total)} ({human_speed(self._tracker.average_bps)} avg)",
                    tag="transfer",
                )
            )
        else:
            pct = self._sent * 100 // self._total if self._total else 0
            self._sink(
                LogEvent(
                    Level.WARNING,
                    f"{self._peer}: transfer aborted at {pct}% ({human_size(self._sent)} of {human_size(self._total)})",
                )
            )


class _LogForwarder(logging.Handler):
    """Forwards tftpy's own WARNING/ERROR records (timeouts, protocol errors)."""

    def __init__(self, sink: EventSink) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        level = Level.ERROR if record.levelno >= logging.ERROR else Level.WARNING
        self._sink(LogEvent(level, f"tftpy: {record.getMessage()}"))


def describe_bind_error(exc: OSError, ip: str, port: int) -> str:
    in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)}
    not_local = {errno.EADDRNOTAVAIL, getattr(errno, "WSAEADDRNOTAVAIL", -1)}
    if isinstance(exc, PermissionError):
        return f"Permission denied binding {ip}:{port}/UDP (ports below 1024 need elevated privileges)."
    if exc.errno in in_use:
        return f"UDP port {port} on {ip} is already in use by another program."
    if exc.errno in not_local:
        return f"{ip} is not assigned to any local interface (did the network change?)."
    return f"Cannot bind {ip}:{port}/UDP: {exc}"


class TftpService:
    """Serve one file over TFTP. ``serve`` blocks: run it on a worker thread."""

    def __init__(self, on_event: EventSink, select_timeout: float = SELECT_TIMEOUT) -> None:
        self._emit = on_event
        self._select_timeout = select_timeout
        self._server: Optional[tftpy.TftpServer] = None

    def serve(
        self, file_path: str, bind_ip: str, port: int = DEFAULT_PORT, policy: ServePolicy = ServePolicy()
    ) -> None:
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            self._fail(f"File not found: {path}")
            return
        name, size = os.path.basename(path), os.path.getsize(path)
        guard = ServeGuard(policy, self._emit, self.stop)

        def open_file(requested, raddress=None, rport=None):
            peer = raddress or "unknown"
            if not guard.client_allowed(raddress):
                return None
            self._log(Level.INFO, f"{peer}: requested '{requested}' -> serving {name} ({human_size(size)})")
            guard.transfer_started()
            try:
                return _ProgressReader(path, peer, self._emit, guard)
            except OSError as exc:
                self._log(Level.ERROR, f"{peer}: cannot open {name}: {exc}")
                guard.transfer_finished(False)
                return None

        def deny_upload(dest, context):
            self._log(Level.WARNING, f"{context.host}: upload request rejected (server is read-only)")
            return None

        forwarder = _LogForwarder(self._emit)
        tftp_logger = logging.getLogger("tftpy")
        done = threading.Event()
        with tempfile.TemporaryDirectory(prefix="nettransfer-tftp-") as empty_root:
            server = tftpy.TftpServer(empty_root, dyn_file_func=open_file, upload_open=deny_upload)
            self._server = server
            tftp_logger.addHandler(forwarder)
            threading.Thread(
                target=self._announce_when_bound,
                args=(server, done, f"{bind_ip}:{port}", name, size),
                daemon=True,
            ).start()
            guard.start_watchdog(done)
            try:
                server.listen(bind_ip, port, timeout=self._select_timeout)
            except OSError as exc:
                self._fail(describe_bind_error(exc, bind_ip, port))
                return
            finally:
                done.set()
                tftp_logger.removeHandler(forwarder)
                self._server = None
        self._log(Level.INFO, "TFTP server stopped")
        self._emit(StateEvent(State.STOPPED))

    def stop(self) -> None:
        """Ask the server loop to exit (effective within ``select_timeout``)."""
        server = self._server
        if server is not None:
            server.stop(now=True)

    # -- internals ---------------------------------------------------------
    def _announce_when_bound(self, server, done: threading.Event, addr: str, name: str, size: int) -> None:
        # tftpy sets is_running only after a successful bind, so this reports
        # "listening" without a racy pre-bind probe of the same port.
        while not done.is_set():
            if server.is_running.wait(0.05):
                self._log(Level.SUCCESS, f"TFTP server listening on {addr} - serving {name} ({human_size(size)})")
                self._emit(StateEvent(State.STARTED, addr))
                return

    def _log(self, level: Level, message: str) -> None:
        self._emit(LogEvent(level, message))

    def _fail(self, message: str) -> None:
        self._log(Level.ERROR, message)
        self._emit(StateEvent(State.FAILED, message))
