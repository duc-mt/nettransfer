"""Shared SSH connect + host-key check + authentication (used by SCP/SFTP and the connection test)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Callable, Optional

import paramiko

from .events import EventSink, Level, LogEvent
from .known_hosts import MATCH, NEW, KnownHostsStore


class HostKeyChangedError(paramiko.SSHException):
    pass


@dataclass(frozen=True)
class SshOptions:
    host: str
    username: str = ""
    password: str = field(default="", repr=False)
    port: int = 22
    key_path: str = ""
    key_passphrase: str = field(default="", repr=False)
    replace_host_key: bool = False
    timeout: float = 15.0


def open_transport(
    opts: SshOptions,
    known_hosts: KnownHostsStore,
    emit: EventSink,
    on_created: Optional[Callable[[paramiko.Transport], None]] = None,
) -> paramiko.Transport:
    """Connect, verify the host key (TOFU) and authenticate. ``on_created`` lets a caller register the
    transport early so a cancel request can interrupt a blocked handshake."""
    sock = socket.create_connection((opts.host, opts.port), timeout=opts.timeout)
    transport: Optional[paramiko.Transport] = None
    try:
        transport = paramiko.Transport(sock)
        if on_created is not None:
            on_created(transport)
        transport.start_client(timeout=opts.timeout)
        _verify_host_key(opts, transport.get_remote_server_key(), known_hosts, emit)
        _authenticate(transport, opts)
        return transport
    except BaseException:
        (transport or sock).close()
        raise


def _verify_host_key(opts: SshOptions, key: paramiko.PKey, store: KnownHostsStore, emit: EventSink) -> None:
    fingerprint, status = store.fingerprint(key), store.check(opts.host, opts.port, key)
    if status == MATCH:
        emit(LogEvent(Level.INFO, f"Host key {fingerprint} matches the stored key"))
    elif status == NEW:
        store.trust(opts.host, opts.port, key)
        emit(LogEvent(Level.INFO, f"New host key {fingerprint} ({key.get_name()}) - trusted on first use and saved"))
    elif opts.replace_host_key:
        store.trust(opts.host, opts.port, key)
        emit(LogEvent(Level.WARNING, f"Host key CHANGED - replaced by {fingerprint} as requested"))
    else:
        raise HostKeyChangedError(
            f"The host key of {opts.host} has CHANGED (now {fingerprint}). This can mean a man-in-the-middle, "
            "or just a replaced/reset device. If you expected this, tick 'Accept changed host key' and retry."
        )


def load_private_key(path: str, passphrase: str) -> paramiko.PKey:
    """paramiko wants a bytes passphrase and reports encrypted-key problems as bare TypeError/ValueError;
    turn those into messages a person can act on."""
    secret = passphrase.encode("utf-8") if passphrase else None
    try:
        return paramiko.PKey.from_path(path, passphrase=secret)
    except TypeError as exc:
        if "not given" in str(exc):
            raise paramiko.PasswordRequiredException("The private key is encrypted") from exc
        if secret is not None:  # a passphrase was typed but the key is not encrypted
            return paramiko.PKey.from_path(path)
        raise
    except ValueError as exc:
        raise paramiko.SSHException("Could not decrypt the private key - wrong passphrase?") from exc


def _authenticate(transport: paramiko.Transport, opts: SshOptions) -> None:
    last: Optional[Exception] = None
    if opts.key_path:
        key = load_private_key(opts.key_path, opts.key_passphrase)
        try:
            transport.auth_publickey(opts.username, key)
            return
        except paramiko.AuthenticationException as exc:
            last = exc
    if opts.password:
        try:
            transport.auth_password(opts.username, opts.password)
            return
        except paramiko.BadAuthenticationType as exc:
            if "keyboard-interactive" not in exc.allowed_types:
                raise
            # Some devices only offer keyboard-interactive: answer every prompt with the password
            transport.auth_interactive(opts.username, lambda _t, _i, prompts: [opts.password] * len(prompts))
            return
        except paramiko.AuthenticationException as exc:
            last = exc
    raise last or paramiko.AuthenticationException("no password or key supplied")
