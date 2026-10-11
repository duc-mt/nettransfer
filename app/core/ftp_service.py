"""FTP / FTPS (explicit TLS) push service. Qt-free: reports through an EventSink."""

from __future__ import annotations

import ftplib
import os
import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import Optional

from .events import EventSink, Level, LogEvent, State, StateEvent
from .formatting import human_size, human_speed
from .progress import ProgressTracker


@dataclass(frozen=True)
class FtpRequest:
    host: str
    username: str
    local_path: str
    password: str = field(default="", repr=False)
    port: int = 21
    remote_path: str = ""
    tls: bool = False  # explicit FTPS (AUTH TLS); certificate is NOT verified (devices use self-signed)
    passive: bool = True
    connect_timeout: float = 15.0


class _Cancelled(Exception):
    pass


class FtpService:
    def __init__(self, on_event: EventSink) -> None:
        self._emit = on_event
        self._cancel = threading.Event()
        self._ftp: Optional[ftplib.FTP] = None

    def push(self, req: FtpRequest) -> None:
        path = os.path.abspath(req.local_path)
        if not os.path.isfile(path):
            self._fail(f"File not found: {path}")
            return
        size, name = os.path.getsize(path), os.path.basename(path)
        remote = req.remote_path.strip()
        target = name if remote in ("", ".") else (remote + name if remote.endswith("/") else remote)
        self._cancel.clear()
        tracker, sent = ProgressTracker(size, self._emit, peer=req.host), 0
        label = "FTPS" if req.tls else "FTP"

        def on_block(block: bytes) -> None:
            nonlocal sent
            if self._cancel.is_set():
                raise _Cancelled()
            sent += len(block)
            tracker.update(sent)

        try:
            self._log(Level.INFO, f"Connecting to {req.host}:{req.port} ({label}) ...")
            if req.tls:
                ftp: ftplib.FTP = ftplib.FTP_TLS(context=ssl._create_unverified_context())
            else:
                ftp = ftplib.FTP()
            self._ftp = ftp
            ftp.connect(req.host, req.port, timeout=req.connect_timeout)
            ftp.login(req.username or "anonymous", req.password)
            if req.tls:
                ftp.prot_p()
                self._log(Level.WARNING, "FTPS certificate is not verified")
            ftp.set_pasv(req.passive)
            self._emit(StateEvent(State.STARTED, req.host))
            self._log(Level.INFO, f"Pushing {name} ({human_size(size)}) to {target} ...")
            with open(path, "rb") as fh:
                ftp.storbinary(f"STOR {target}", fh, blocksize=32768, callback=on_block)
            tracker.finish()
            self._log(
                Level.SUCCESS,
                f"Pushed {name} to {req.host} via {label} in {tracker.elapsed:.1f}s "
                f"({human_speed(tracker.average_bps)} avg)",
                tag="transfer",
            )
            self._emit(StateEvent(State.FINISHED, req.host))
        except Exception as exc:
            if self._cancel.is_set() or isinstance(exc, _Cancelled):
                self._log(Level.WARNING, "Transfer cancelled")
                self._emit(StateEvent(State.STOPPED, "cancelled"))
            else:
                self._fail(self._explain(exc, req))
        finally:
            ftp = self._ftp
            if ftp is not None:
                try:
                    ftp.close()
                except OSError:
                    pass
            self._ftp = None

    def cancel(self) -> None:
        self._cancel.set()
        ftp = self._ftp
        if ftp is not None and ftp.sock is not None:
            try:
                ftp.sock.close()  # interrupts a blocked connect/transfer
            except OSError:
                pass

    @staticmethod
    def _explain(exc: Exception, req: FtpRequest) -> str:
        if isinstance(exc, ftplib.error_perm):
            text = str(exc)
            if text.startswith("530"):
                return f"FTP login failed for '{req.username or 'anonymous'}': {text}"
            return f"FTP server refused the request: {text} (check the remote path and write permission)"
        if isinstance(exc, ftplib.error_temp):
            return f"FTP server reported a temporary error: {exc}"
        if isinstance(exc, ConnectionRefusedError):
            return f"Connection refused by {req.host}:{req.port} - is FTP enabled?"
        if isinstance(exc, socket.gaierror):
            return f"Cannot resolve host '{req.host}'."
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return f"Timed out talking to {req.host}:{req.port}."
        if isinstance(exc, ssl.SSLError):
            return f"TLS error: {exc}. Try plain FTP if the device has no FTPS support."
        if isinstance(exc, (OSError, EOFError)):
            return f"Network error: {exc}"
        return f"{type(exc).__name__}: {exc}"

    def _log(self, level: Level, message: str, tag: str = "") -> None:
        self._emit(LogEvent(level, message, tag=tag))

    def _fail(self, message: str) -> None:
        self._log(Level.ERROR, message)
        self._emit(StateEvent(State.FAILED, message))
