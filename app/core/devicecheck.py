"""Check a device without sending anything: compare the digest of a file already on it, and/or confirm its running
software version. This is how you verify platforms that *pull* their image (FortiOS, Aruba AOS-CX, PAN-OS, Ruijie ...)
after you served it, and it works for any vendor profile with a verify or version command."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional

import paramiko

from .events import EventSink, Level, LogEvent, State, StateEvent
from .known_hosts import KnownHostsStore
from .scp_service import explain_error
from .ssh_session import JumpHost, SshOptions, open_transport
from .vendors import UnknownVendorError, device_path, get_vendor
from .verify import VerifyOptions, VerifyResult, check_version, verify_remote


@dataclass(frozen=True)
class DeviceCheck:
    host: str
    username: str
    vendor: str
    password: str = field(default="", repr=False)
    port: int = 22
    key_path: str = ""
    key_passphrase: str = field(default="", repr=False)
    replace_host_key: bool = False
    connect_timeout: float = 15.0
    jump: Optional[JumpHost] = None
    file: str = ""  # local file whose digest is compared with the device's ("" = no digest check)
    remote_path: str = ""  # where it is on the device (a directory ending in / or :, or the full path)
    verify: Optional[VerifyOptions] = None
    expect_version: str = ""  # text that must appear in the vendor's version command output

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


class DeviceCheckService:
    def __init__(self, emit: EventSink, known_hosts: KnownHostsStore) -> None:
        self._emit, self._known_hosts = emit, known_hosts
        self._lock = threading.Lock()
        self._transport: Optional[paramiko.Transport] = None
        self._cancel = threading.Event()
        self.results: List[VerifyResult] = []

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            transport = self._transport
        if transport is not None:
            transport.close()

    def _remember(self, transport: paramiko.Transport) -> None:
        with self._lock:
            self._transport = transport
        if self._cancel.is_set():
            transport.close()

    def _log(self, level: Level, message: str, tag: str = "") -> None:
        self._emit(LogEvent(level, message, tag=tag))

    def run(self, req: DeviceCheck) -> None:
        transport: Optional[paramiko.Transport] = None
        try:
            vendor = get_vendor(req.vendor)
            if not req.file and not req.expect_version:
                raise ValueError("nothing to check: give a file to compare and/or --expect-version")
            if req.file and not os.path.isfile(req.file):
                raise ValueError(f"'{req.file}' is not a file")
            self._log(Level.INFO, f"Connecting to {req.username}@{req.host}:{req.port} ({vendor.label}) ...")
            transport = open_transport(req.ssh_options(), self._known_hosts, self._emit, self._remember)
            ok = True
            if req.file:
                name = os.path.basename(req.file)
                target = device_path(req.remote_path, name, vendor)
                self._log(Level.INFO, f"Verifying {target} on the device (this can take minutes for a large image) ...")
                result = verify_remote(transport, req.file, target, name, req.verify or VerifyOptions(), vendor)
                self._report(result, f"{name} on {req.host}")
                ok = ok and result.ok
            if req.expect_version:
                result = check_version(transport, vendor, req.expect_version)
                self._report(result, f"software version on {req.host}")
                ok = ok and result.ok
            self._emit(StateEvent(State.FINISHED if ok else State.FAILED, "" if ok else "verification failed"))
        except (UnknownVendorError, ValueError) as exc:
            self._log(Level.ERROR, str(exc))
            self._emit(StateEvent(State.FAILED, str(exc)))
        except Exception as exc:
            if self._cancel.is_set():
                self._emit(StateEvent(State.STOPPED, "cancelled"))
            else:
                message = explain_error(exc, req.host, req.port, req.username)
                self._log(Level.ERROR, message)
                self._emit(StateEvent(State.FAILED, message))
        finally:
            if transport is not None:
                transport.close()

    def _report(self, result: VerifyResult, subject: str) -> None:
        self.results.append(result)
        level = Level.SUCCESS if result.ok else Level.ERROR
        self._log(level, f"{result.status}: {subject} - {result.detail}", tag="verify")
