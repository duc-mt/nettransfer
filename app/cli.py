"""Headless command line: the same core services without Qt (jump hosts, scripts, CI).

Passwords are never accepted as command-line arguments (they would show in ``ps``): use an
environment variable (--password-env) or the interactive prompt.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time
from typing import Optional, Sequence

from . import __version__
from .core.batch import BatchFileError, BatchRunner, load_targets_csv, write_results_csv
from .core.events import BatchEvent, Event, Level, LogEvent, ProgressEvent, State, StateEvent
from .core.formatting import human_size, human_speed
from .core.hashing import HashService
from .core.http_service import DEFAULT_PORT as HTTP_PORT
from .core.http_service import HttpService
from .core.known_hosts import KnownHostsStore
from .core.precheck import ConnectionTester
from .core.requests import PushDefaults, default_port, make_service_factory
from .core.serve_policy import ServePolicy, parse_allowlist
from .core.tftp_service import DEFAULT_PORT as TFTP_PORT
from .core.tftp_service import TftpService

COMMANDS = ("serve", "push", "batch", "test", "hash")
OK, FAILED, USAGE = 0, 1, 2


class ConsoleSink:
    """Thread-safe event printer; progress is a single rewritten line on a terminal."""

    def __init__(self, stream=None) -> None:
        self._out = stream or sys.stdout
        self._tty = self._out.isatty()
        self._lock = threading.Lock()
        self._on_progress_line = False
        self.failed = False

    def __call__(self, event: Event) -> None:
        with self._lock:
            if isinstance(event, LogEvent):
                self._end_progress()
                stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
                print(f"[{stamp}] {event.level.value.upper():<7} {event.message}", file=self._out, flush=True)
            elif isinstance(event, ProgressEvent) and self._tty:
                pct = 100 if event.total <= 0 else min(100, event.sent * 100 // event.total)
                self._out.write(
                    f"\r  {pct:3d}%  {human_speed(event.bps)}  {human_size(event.sent)}/{human_size(event.total)}   "
                )
                self._out.flush()
                self._on_progress_line = True
            elif isinstance(event, BatchEvent) and event.status in ("done", "failed", "cancelled"):
                self._end_progress()
                tag = {"done": " OK ", "failed": "FAIL", "cancelled": "STOP"}[event.status]
                print(
                    f"[{tag}] {event.host}" + (f" - {event.detail}" if event.detail else ""), file=self._out, flush=True
                )
            elif isinstance(event, StateEvent) and event.state is State.FAILED:
                self.failed = True

    def _end_progress(self) -> None:
        if self._on_progress_line:
            self._out.write("\n")
            self._on_progress_line = False


def _secret(env_name: Optional[str], prompt: str, needed: bool) -> str:
    if env_name:
        value = os.environ.get(env_name)
        if value is None:
            raise SystemExit(f"error: environment variable {env_name} is not set")
        return value
    return getpass.getpass(prompt) if needed and sys.stdin.isatty() else ""


def _policy(args) -> ServePolicy:
    try:
        allowed = parse_allowlist(args.allow or "")
    except ValueError as exc:
        raise SystemExit(f"error: --allow: {exc}") from None
    return ServePolicy(args.once, float(args.idle) if args.idle else None, allowed)


def _run_blocking(service, job, sink: ConsoleSink, stop_name: str = "stop") -> int:
    """Run ``job`` on a thread so Ctrl-C can stop/cancel the service cleanly."""
    thread = threading.Thread(target=job, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.2)
    except KeyboardInterrupt:
        print("\nInterrupted - stopping ...", file=sys.stderr)
        getattr(service, stop_name)()
        thread.join(15)
        return 130
    return FAILED if sink.failed else OK


def cmd_serve(args) -> int:
    from .platform.detect import detect_platform
    from .platform.network import default_route_ip
    from .platform.privileges import probe_udp_bind, remedies

    sink = ConsoleSink()
    ip = args.ip or default_route_ip() or "0.0.0.0"
    policy = _policy(args)
    if args.protocol == "tftp":
        port = args.port or TFTP_PORT
        probe = probe_udp_bind(ip, port)
        if not probe.ok:
            sink(LogEvent(Level.ERROR, f"Cannot bind UDP {ip}:{port}: {probe.detail}"))
            for line in remedies(detect_platform(), probe, port):
                print(f"    {line}", file=sys.stderr)
            return FAILED
        service = TftpService(sink)
        return _run_blocking(service, lambda: service.serve(args.file, ip, port, policy), sink)
    service = HttpService(sink)
    return _run_blocking(
        service, lambda: service.serve(args.file, ip, args.port or HTTP_PORT, policy, args.https), sink
    )


def _defaults(args, password: str, passphrase: str) -> PushDefaults:
    return PushDefaults(
        args.protocol,
        args.user or "",
        password,
        args.key or "",
        passphrase,
        args.port,
        args.remote or "",
        args.accept_changed_key,
    )


def _store() -> KnownHostsStore:
    from .platform.paths import data_dir, fix_owner

    return KnownHostsStore(data_dir() / "known_hosts", fix_owner)


def cmd_push(args) -> int:
    sink = ConsoleSink()
    password = _secret(
        args.password_env,
        f"Password for {args.user or 'device'}@{args.host}: ",
        args.protocol in ("ftp", "ftps", "scp", "sftp") and not args.key,
    )
    passphrase = _secret(args.key_passphrase_env, "", False)
    service = make_service_factory(args.protocol, _store())(sink)
    request = _defaults(args, password, passphrase).build(args.host, args.file)
    return _run_blocking(service, lambda: service.push(request), sink, "cancel")


def cmd_batch(args) -> int:
    sink = ConsoleSink()
    try:
        targets = load_targets_csv(args.targets)
    except (BatchFileError, OSError) as exc:
        for line in getattr(exc, "errors", [str(exc)]):
            print(f"error: {args.targets}: {line}", file=sys.stderr)
        return USAGE
    password = _secret(args.password_env, "Default password for devices without their own: ", True)
    passphrase = _secret(args.key_passphrase_env, "", False)
    defaults = _defaults(args, password, passphrase)
    runner = BatchRunner(
        sink,
        make_service_factory(args.protocol, _store()),
        lambda t: defaults.build(t.host, args.file, t),
        args.workers,
    )
    code = _run_blocking(runner, lambda: runner.run(targets), sink, "cancel")
    if args.results:
        write_results_csv(args.results, targets, runner.results)
        print(f"Results written to {args.results}")
    bad = any(s != "done" for s, _ in runner.results.values()) or len(runner.results) < len(targets)
    return code or (FAILED if bad else OK)


def cmd_test(args) -> int:
    sink = ConsoleSink()
    port = args.port or default_port(args.protocol)
    ConnectionTester(sink, _store()).run(args.host, port, args.protocol)
    return FAILED if sink.failed else OK


def cmd_hash(args) -> int:
    sink = ConsoleSink()
    HashService(sink).run(args.file)
    return FAILED if not os.path.isfile(args.file) else OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nettransfer", description="Send files to network devices (headless mode).")
    parser.add_argument("--version", action="version", version=f"nettransfer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve a file for devices to pull (TFTP or HTTP/HTTPS)")
    serve.add_argument("protocol", choices=["tftp", "http"])
    serve.add_argument("file")
    serve.add_argument("--ip", help="local IP to bind (default: the default-route address)")
    serve.add_argument("--port", type=int, help=f"default {TFTP_PORT} (tftp) / {HTTP_PORT} (http)")
    serve.add_argument("--https", action="store_true", help="http only: TLS with a self-signed certificate")
    serve.add_argument("--once", action="store_true", help="stop after the first successful transfer")
    serve.add_argument("--idle", type=float, metavar="SECONDS", help="stop after this long without a transfer")
    serve.add_argument("--allow", metavar="CIDRS", help="only these clients, e.g. 10.0.0.0/24,192.168.1.5")
    serve.set_defaults(func=cmd_serve)

    def push_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("-u", "--user", default="", help="username (FTP: blank = anonymous)")
        p.add_argument("--password-env", metavar="VAR", help="read the password from this environment variable")
        p.add_argument("--key", metavar="PATH", help="SSH private key (scp/sftp)")
        p.add_argument("--key-passphrase-env", metavar="VAR")
        p.add_argument("--remote", default="", help="remote path, e.g. flash: or /var/tmp/ (blank = device default)")
        p.add_argument("--port", type=int)
        p.add_argument("--accept-changed-key", action="store_true", help="replace a changed SSH host key")

    protocols = ["scp", "sftp", "ftp", "ftps"]
    push = sub.add_parser("push", help="push a file to one device")
    push.add_argument("protocol", choices=protocols)
    push.add_argument("file")
    push.add_argument("host")
    push_options(push)
    push.set_defaults(func=cmd_push)

    batch = sub.add_parser("batch", help="push a file to many devices listed in a CSV")
    batch.add_argument("protocol", choices=protocols)
    batch.add_argument("file")
    batch.add_argument("--targets", required=True, help="CSV with columns host[,username,password,remote_path,port]")
    batch.add_argument("--workers", type=int, default=4)
    batch.add_argument("--results", metavar="CSV", help="write a per-device result file")
    push_options(batch)
    batch.set_defaults(func=cmd_batch)

    test = sub.add_parser("test", help="check reachability/banner/host key of a device (no login)")
    test.add_argument("protocol", choices=protocols)
    test.add_argument("host")
    test.add_argument("--port", type=int)
    test.set_defaults(func=cmd_test)

    hashing = sub.add_parser("hash", help="print MD5 and SHA-256 of a file")
    hashing.add_argument("file")
    hashing.set_defaults(func=cmd_hash)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
