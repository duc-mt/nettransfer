"""'Test connection': reachability, banner and (SSH) host-key status - no credentials needed."""

from __future__ import annotations

import ftplib
import socket
import time
from typing import Optional

import paramiko

from .events import EventSink, Level, LogEvent, State, StateEvent
from .known_hosts import CHANGED, MATCH, KnownHostsStore
from .scp_service import explain_error


class ConnectionTester:
    def __init__(self, on_event: EventSink, known_hosts: Optional[KnownHostsStore] = None) -> None:
        self._emit = on_event
        self._known_hosts = known_hosts or KnownHostsStore()

    def run(self, host: str, port: int, protocol: str = "scp", timeout: float = 8.0) -> None:
        protocol = protocol.lower()
        self._log(Level.INFO, f"Testing {host}:{port} ({protocol.upper()}) ...")
        try:
            started = time.monotonic()
            if protocol in ("scp", "sftp"):
                self._ssh(host, port, timeout, started)
            else:
                self._ftp(host, port, timeout, started, tls=protocol == "ftps")
        except Exception as exc:
            message = explain_error(exc, host, port, "")
            self._log(Level.ERROR, message)
            self._emit(StateEvent(State.FAILED, message))
            return
        self._emit(StateEvent(State.FINISHED, host))

    def _ssh(self, host: str, port: int, timeout: float, started: float) -> None:
        sock = socket.create_connection((host, port), timeout=timeout)
        self._log(Level.SUCCESS, f"TCP {port} open ({(time.monotonic() - started) * 1000:.0f} ms)")
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
            self._log(Level.INFO, f"Server: {transport.remote_version}")
            status = self._known_hosts.check(host, port, key)
            note = {MATCH: "matches the stored key", CHANGED: "CHANGED since last time!"}.get(status, "not seen before")
            self._log(
                Level.WARNING if status == CHANGED else Level.SUCCESS,
                f"Host key {self._known_hosts.fingerprint(key)} ({key.get_name()}) - {note}",
            )
        finally:
            transport.close()

    def _ftp(self, host: str, port: int, timeout: float, started: float, tls: bool) -> None:
        ftp = ftplib.FTP()
        try:
            ftp.connect(host, port, timeout=timeout)
            self._log(Level.SUCCESS, f"TCP {port} open ({(time.monotonic() - started) * 1000:.0f} ms)")
            self._log(Level.INFO, f"Server: {ftp.getwelcome()}")
        finally:
            ftp.close()

    def _log(self, level: Level, message: str) -> None:
        self._emit(LogEvent(level, message))
