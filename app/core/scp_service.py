"""SCP / SFTP push service (paramiko + scp). Qt-free: reports through an EventSink."""

from __future__ import annotations

import os
import socket
import stat
import threading
from dataclasses import dataclass, field
from typing import Optional

import paramiko
from scp import SCPClient, SCPException

from .events import EventSink, Level, LogEvent, State, StateEvent
from .formatting import human_size, human_speed
from .known_hosts import KnownHostsStore
from .progress import ProgressTracker
from .ssh_session import HostKeyChangedError, SshOptions, open_transport


@dataclass(frozen=True)
class ScpRequest:
    host: str
    username: str
    local_path: str
    password: str = field(default="", repr=False)  # never shown in repr/logs
    port: int = 22
    remote_path: str = ""  # blank -> device default directory
    connect_timeout: float = 15.0
    protocol: str = "scp"  # "scp" | "sftp"
    key_path: str = ""
    key_passphrase: str = field(default="", repr=False)
    replace_host_key: bool = False

    def ssh_options(self) -> SshOptions:
        return SshOptions(
            self.host,
            self.username,
            self.password,
            self.port,
            self.key_path,
            self.key_passphrase,
            self.replace_host_key,
            self.connect_timeout,
        )


class _Cancelled(Exception):
    pass


def explain_error(exc: Exception, host: str, port: int, username: str) -> str:
    if isinstance(exc, HostKeyChangedError):
        return str(exc)
    if isinstance(exc, paramiko.PasswordRequiredException):
        return "The private key is encrypted - enter its passphrase."
    if isinstance(exc, paramiko.AuthenticationException):
        return (
            f"Authentication failed for '{username}'. Check the credentials; Cisco IOS also needs "
            f"'ip scp server enable' and a privilege-15 user."
        )
    if isinstance(exc, (ConnectionRefusedError, paramiko.ssh_exception.NoValidConnectionsError)):
        return f"Connection refused by {host}:{port} - is the service enabled and reachable?"
    if isinstance(exc, socket.gaierror):
        return f"Cannot resolve host '{host}'."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"Timed out talking to {host}:{port}."
    if isinstance(exc, paramiko.SSHException):
        return f"SSH negotiation failed: {exc}"
    if isinstance(exc, SCPException):
        return f"SCP error: {exc}. Check the remote path/permissions and that SCP is enabled on the device."
    if isinstance(exc, FileNotFoundError):
        return f"File or key not found: {exc.filename or exc}"
    if isinstance(exc, OSError):
        return f"Network error: {exc}"
    return f"{type(exc).__name__}: {exc}"


class ScpService:
    """Push one file to a device. ``push`` blocks: run it on a worker thread."""

    def __init__(self, on_event: EventSink, known_hosts: Optional[KnownHostsStore] = None) -> None:
        self._emit = on_event
        self._known_hosts = known_hosts or KnownHostsStore()
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._transport: Optional[paramiko.Transport] = None

    def push(self, req: ScpRequest) -> None:
        path = os.path.abspath(req.local_path)
        if not os.path.isfile(path):
            self._fail(f"File not found: {path}")
            return
        size, name = os.path.getsize(path), os.path.basename(path)
        self._cancel.clear()
        tracker = ProgressTracker(size, self._emit, peer=req.host)

        def on_progress(sent: int) -> None:
            if self._cancel.is_set():
                raise _Cancelled()
            tracker.update(sent)

        label = req.protocol.upper()
        try:
            self._log(Level.INFO, f"Connecting to {req.username}@{req.host}:{req.port} ({label}) ...")
            transport = open_transport(req.ssh_options(), self._known_hosts, self._emit, self._remember)
            self._emit(StateEvent(State.STARTED, req.host))
            if req.protocol == "sftp":
                target = self._push_sftp(transport, path, name, req.remote_path.strip(), on_progress)
            else:
                target = req.remote_path.strip() or "."
                self._log(Level.INFO, f"Pushing {name} ({human_size(size)}) to {target} ...")
                with SCPClient(transport, socket_timeout=30.0, progress=lambda _f, _t, sent: on_progress(sent)) as scp:
                    scp.put(path, target)
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
                self._fail(explain_error(exc, req.host, req.port, req.username))
        finally:
            with self._lock:
                transport = self._transport
                self._transport = None
            if transport is not None:
                transport.close()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            transport = self._transport
        if transport is not None:
            transport.close()  # also interrupts a blocked handshake/auth/transfer

    # -- internals ---------------------------------------------------------
    def _remember(self, transport: paramiko.Transport) -> None:
        with self._lock:
            self._transport = transport
            cancelled = self._cancel.is_set()
        if cancelled:
            transport.close()

    def _push_sftp(self, transport, path: str, name: str, remote: str, on_progress) -> str:
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            sftp.get_channel().settimeout(30.0)
            target = self._sftp_target(sftp, remote, name)
            self._log(Level.INFO, f"Pushing {name} ({human_size(os.path.getsize(path))}) to {target} ...")
            sftp.put(path, target, callback=lambda sent, _total: on_progress(sent), confirm=True)
            return target
        finally:
            sftp.close()

    @staticmethod
    def _sftp_target(sftp: paramiko.SFTPClient, remote: str, name: str) -> str:
        if remote in ("", "."):
            return name
        if remote.endswith("/"):
            return remote + name
        try:
            if stat.S_ISDIR(sftp.stat(remote).st_mode or 0):
                return remote.rstrip("/") + "/" + name
        except OSError:
            pass  # not an existing directory: treat as the full target file path
        return remote

    def _log(self, level: Level, message: str, tag: str = "") -> None:
        self._emit(LogEvent(level, message, tag=tag))

    def _fail(self, message: str) -> None:
        self._log(Level.ERROR, message)
        self._emit(StateEvent(State.FAILED, message))
