"""Opt-in post-push actions: set the boot image and/or reload (now or scheduled) after a verified push.

Safe by construction:

* the default is a *dry run* that only prints the commands;
* applying them needs ``execute=True`` **and** ``confirmed=True``: the front-end (CLI prompt / GUI dialog) sets
  ``confirmed`` only after the operator has agreed, so no caller can change a device by accident;
* the scp/sftp service refuses to execute unless verification is enabled too, so a corrupt image can never be made the
  boot image by this tool;
* a scheduled reload (``reload in N``) is available where the platform has one; otherwise it is refused, not emulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import paramiko

from .events import EventSink, Level, LogEvent
from .remote import RemoteError, Shell, first_error
from .vendors import Vendor, is_safe_device_path

MAX_RELOAD_MINUTES = 24 * 60


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class InstallOptions:
    execute: bool = False  # False = dry run: only show the commands
    reload: bool = False  # reload now (needs execute)
    timeout: float = 120.0  # per command; 'write memory' / software add can take a while
    set_boot: bool = True  # set the boot image to the pushed file
    reload_in: Optional[int] = None  # schedule a reload this many minutes from now (instead of reload=True)
    confirmed: bool = False  # the operator explicitly agreed to apply the plan (required with execute)


def describe(opts: InstallOptions) -> str:
    """One-line summary for confirmation prompts and logs."""
    parts = []
    if opts.set_boot:
        parts.append("set the boot image to the pushed file")
    if opts.reload_in is not None:
        parts.append(f"schedule a reload in {opts.reload_in} minute(s)")
    elif opts.reload:
        parts.append("RELOAD the device immediately")
    return ", ".join(parts) or "nothing"


def plan(vendor: Vendor, device_file: str, name: str, opts: InstallOptions) -> List[str]:
    if opts.reload and opts.reload_in is not None:
        raise InstallError("choose either an immediate reload or a scheduled one, not both")
    if opts.reload_in is not None and not 1 <= opts.reload_in <= MAX_RELOAD_MINUTES:
        raise InstallError(f"the reload delay must be between 1 and {MAX_RELOAD_MINUTES} minutes")
    lines: List[str] = []
    if opts.set_boot:
        if not vendor.install:
            raise InstallError(f"there is no boot-image profile for {vendor.label}")
        if not (is_safe_device_path(device_file) and is_safe_device_path(name)):
            raise InstallError(f"refusing to build install commands from '{device_file}' (unsafe characters)")
        lines += [line.format(path=device_file, file=name) for line in vendor.install]
    if opts.reload_in is not None:
        if not vendor.reload_in:
            raise InstallError(
                f"a scheduled reload is not available for {vendor.label}: reload now, or schedule it on the device"
            )
        minutes = opts.reload_in
        lines += [line.format(minutes=minutes, hhmm=f"{minutes // 60}:{minutes % 60:02d}") for line in vendor.reload_in]
    elif opts.reload:
        if not vendor.reload:
            raise InstallError(f"there is no reload command for {vendor.label}")
        lines += list(vendor.reload)
    if not lines:
        raise InstallError("nothing to do: choose to set the boot image and/or to reload")
    return lines


def run_plan(
    transport: paramiko.Transport, vendor: Vendor, lines: List[str], opts: InstallOptions, emit: EventSink
) -> None:
    # only an *immediate* reload makes the device drop the session; a scheduled one leaves it answering
    reload_cmd = vendor.reload[0] if opts.reload and opts.reload_in is None and vendor.reload else None
    shell = Shell(transport, timeout=opts.timeout)
    try:
        shell.read_until_prompt()  # banner / first prompt
        reloading = False
        for line in lines:
            if reload_cmd is not None and line == reload_cmd:
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
