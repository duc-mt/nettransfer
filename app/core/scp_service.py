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
from .install import InstallError, InstallOptions, plan, run_plan
from .known_hosts import KnownHostsStore
from .preflight import check_free_space, extension_warning
from .progress import ProgressTracker
from .remote import RemoteError
from .retry import is_transient
from .ssh_session import HostKeyChangedError, JumpHost, SshOptions, open_transport
from .vendors import Vendor, device_path, get_vendor
from .verify import VerifyOptions, verify_remote


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
    jump: Optional[JumpHost] = None  # reach the device through an SSH bastion
    vendor: str = ""  # profile id (see vendors.py); enables vendor-aware preflight / verify / install
    preflight: bool = False  # check the image name and the free space on the device before sending
    verify: Optional[VerifyOptions] = None  # compare the device's digest of the file with the local one
    install: Optional[InstallOptions] = None  # set the boot image afterwards (dry run unless install.execute)
    resume: bool = False  # sftp only: continue a partial upload instead of starting over

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
            self.jump,
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
            vendor = get_vendor(req.vendor) if req.vendor else None
            self._check_options(req, vendor)
            self._log(Level.INFO, f"Connecting to {req.username}@{req.host}:{req.port} ({label}) ...")
            transport = open_transport(req.ssh_options(), self._known_hosts, self._emit, self._remember)
            device_file = device_path(req.remote_path, name, vendor)
            if req.preflight and not self._preflight(transport, vendor, device_file, name, size):
                return
            self._emit(StateEvent(State.STARTED, req.host))
            if req.protocol == "sftp":
                target = self._push_sftp(transport, path, name, req.remote_path.strip(), on_progress, req.resume)
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
            if req.verify is not None and not self._verify(transport, req, vendor, path, device_file, name):
                return
            if req.install is not None and not self._install(transport, req, vendor, device_file, name):
                return
            self._emit(StateEvent(State.FINISHED, req.host))
        except Exception as exc:
            if self._cancel.is_set() or isinstance(exc, _Cancelled):
                self._log(Level.WARNING, "Transfer cancelled")
                self._emit(StateEvent(State.STOPPED, "cancelled"))
            else:
                self._fail(explain_error(exc, req.host, req.port, req.username), is_transient(exc))
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

    def _check_options(self, req: ScpRequest, vendor: Optional[Vendor]) -> None:
        if req.resume and req.protocol != "sftp":
            raise ValueError("--resume needs SFTP: the SCP protocol cannot continue a partial upload")
        if req.install is not None and req.install.execute and req.verify is None:
            raise ValueError("refusing to change the boot image of an unverified upload: enable verification as well")
        if req.install is not None and (vendor is None or not vendor.install):
            raise ValueError("install needs a vendor with an install profile (see --vendor)")

    def _preflight(self, transport, vendor: Optional[Vendor], device_file: str, name: str, size: int) -> bool:
        warning = extension_warning(vendor, name)
        if warning:
            self._log(Level.WARNING, warning)
        result = check_free_space(transport, vendor, device_file, size)
        if not result.known:
            self._log(Level.INFO, result.detail)
        elif result.free < size:
            self._fail(f"Not enough space on the device: {result.detail}", False)
            return False
        else:
            self._log(Level.INFO, f"Pre-flight: {result.detail}")
        return True

    def _verify(
        self, transport, req: ScpRequest, vendor: Optional[Vendor], path: str, device_file: str, name: str
    ) -> bool:
        self._log(Level.INFO, f"Verifying {device_file} on the device (this can take minutes for a large image) ...")
        result = verify_remote(transport, path, device_file, name, req.verify, vendor)
        if not result.ok:
            self._fail(f"Verification failed: {result.detail}", transient=bool(result.remote))
            return False
        self._log(Level.SUCCESS, f"Verified {name} on {req.host}: {result.detail}", tag="verify")
        return True

    def _install(self, transport, req: ScpRequest, vendor: Vendor, device_file: str, name: str) -> bool:
        opts = req.install
        try:
            lines = plan(vendor, device_file, name, opts.reload)
            if not opts.execute:
                self._log(Level.INFO, f"Install plan for {vendor.label} (dry run - add --execute to apply):")
                for line in lines:
                    self._log(Level.INFO, f"  > {line or '<Enter>'}")
                return True
            self._log(Level.WARNING, f"Applying the install plan on {req.host}:")
            run_plan(transport, vendor, lines, opts, self._emit)
            self._log(Level.SUCCESS, f"Install plan applied on {req.host}", tag="install")
            return True
        except (InstallError, RemoteError) as exc:
            self._fail(f"Install failed: {exc}", False)
            return False

    def _push_sftp(self, transport, path: str, name: str, remote: str, on_progress, resume: bool = False) -> str:
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            sftp.get_channel().settimeout(30.0)
            target = self._sftp_target(sftp, remote, name)
            size = os.path.getsize(path)
            self._log(Level.INFO, f"Pushing {name} ({human_size(size)}) to {target} ...")
            offset = self._resume_offset(sftp, target, size) if resume else 0
            if offset:
                self._log(Level.INFO, f"Resuming at {human_size(offset)} of the partial remote file")
                self._sftp_append(sftp, path, target, offset, size, on_progress)
            else:
                sftp.put(path, target, callback=lambda sent, _total: on_progress(sent), confirm=True)
            return target
        finally:
            sftp.close()

    @staticmethod
    def _resume_offset(sftp: paramiko.SFTPClient, target: str, size: int) -> int:
        """Size of a shorter remote file to continue from; 0 = start over (nothing there, or not a valid prefix)."""
        try:
            remote = sftp.stat(target).st_size or 0
        except OSError:
            return 0
        return remote if 0 < remote < size else 0

    @staticmethod
    def _sftp_append(sftp: paramiko.SFTPClient, path: str, target: str, offset: int, size: int, on_progress) -> None:
        sent = offset
        with open(path, "rb") as local, sftp.file(target, "ab") as remote:
            local.seek(offset)
            remote.set_pipelined(True)
            while True:
                block = local.read(32768)
                if not block:
                    break
                remote.write(block)
                sent += len(block)
                on_progress(sent)
        if (sftp.stat(target).st_size or 0) != size:
            raise OSError("size mismatch after resuming: the remote file is not the same size as the local one")

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

    def _fail(self, message: str, transient: bool = False) -> None:
        self._log(Level.ERROR, message)
        self._emit(StateEvent(State.FAILED, message, transient))
