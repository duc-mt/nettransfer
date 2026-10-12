"""Which failures are worth retrying? Network hiccups yes; wrong credentials, changed host keys, missing files no."""

from __future__ import annotations

import ftplib
import socket

import paramiko

_PERMANENT = (
    paramiko.AuthenticationException,
    paramiko.PasswordRequiredException,
    FileNotFoundError,
    PermissionError,
    socket.gaierror,
    ftplib.error_perm,
)
_TRANSIENT_SSH_HINTS = ("banner", "eof", "reset", "closed", "timed out", "socket is closed")


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _PERMANENT) or type(exc).__name__ == "HostKeyChangedError":
        return False
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, EOFError, ftplib.error_temp)):
        return True
    if isinstance(exc, paramiko.SSHException):
        return any(hint in str(exc).lower() for hint in _TRANSIENT_SSH_HINTS)
    return isinstance(exc, OSError)
