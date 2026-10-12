"""Opt-in 'push and install': set the boot image (and optionally reload) after a verified push.

Safe by construction: the default is a *dry run* that only prints the commands; execution needs ``execute=True`` and a
reload needs ``reload=True`` on top; the scp/sftp service refuses to execute unless verification is also enabled, so a
corrupt image can never be made the boot image by this tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import paramiko

from .events import EventSink, Level, LogEvent
from .remote import RemoteError, Shell, first_error
from .vendors import Vendor, is_safe_device_path


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class InstallOptions:
    execute: bool = False  # False = dry run: only show the commands
    reload: bool = False  # also reload the device (needs execute)
    timeout: float = 120.0  # per command; 'write memory' / software add can take a while


def plan(vendor: Vendor, device_file: str, name: str, reload: bool) -> List[str]:
    if not vendor.install:
        raise InstallError(f"there is no install profile for {vendor.label}")
    if not (is_safe_device_path(device_file) and is_safe_device_path(name)):
        raise InstallError(f"refusing to build install commands from '{device_file}' (unsafe characters)")
    lines = [line.format(path=device_file, file=name) for line in vendor.install]
    if reload:
        lines += list(vendor.reload)
    return lines


def run_plan(
    transport: paramiko.Transport, vendor: Vendor, lines: List[str], opts: InstallOptions, emit: EventSink
) -> None:
    shell = Shell(transport, timeout=opts.timeout)
    try:
        shell.read_until_prompt()  # banner / first prompt
        reloading = False
        for line in lines:
            if line in vendor.reload[:1]:
                reloading = True
            emit(LogEvent(Level.INFO, f"  > {line or '<Enter>'}"))
            try:
                reply = shell.send_line(line)
            except RemoteError:
                if reloading:  # the device went away mid-reload: that is the expected outcome
                    emit(LogEvent(Level.INFO, "Device stopped answering - reload in progress"))
                    return
                raise
            problem = first_error(reply)
            if problem:
                raise InstallError(f"'{line}' was rejected by the device: {problem}")
            if shell.closed:
                if reloading:
                    emit(LogEvent(Level.INFO, "Session closed by the device - reload in progress"))
                    return
                raise InstallError("the device closed the session before the plan finished")
    finally:
        shell.close()
