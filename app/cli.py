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
from .core.audit import AuditLog, load_or_create_key, verify_chain
from .core.batch import BatchFileError, BatchRunner, write_results_csv
from .core.credentials import CredentialStore, KeyringUnavailableError
from .core.events import BatchEvent, Event, Level, LogEvent, ProgressEvent, State, StateEvent
from .core.formatting import human_size, human_speed
from .core.hashing import HashService
from .core.history import load_history, render_html, text_summary
from .core.http_service import DEFAULT_PORT as HTTP_PORT
from .core.http_service import HttpService, is_ipv6
from .core.install import InstallOptions
from .core.inventory import filter_targets, load_inventory
from .core.known_hosts import KnownHostsStore
from .core.precheck import ConnectionTester
from .core.requests import PushDefaults, default_port, make_service_factory
from .core.serve_policy import ServePolicy, parse_allowlist
from .core.ssh_session import parse_jump
from .core.tftp_service import DEFAULT_PORT as TFTP_PORT
from .core.tftp_service import TftpService
from .core.vendors import UnknownVendorError, get_vendor
from .core.verify import VerifyOptions, check_verify_template

COMMANDS = ("serve", "push", "batch", "test", "hash", "interfaces", "credentials", "audit", "history", "web")
OK, FAILED, USAGE = 0, 1, 2


class ConsoleSink:
    """Thread-safe event printer; progress is a single rewritten line on a terminal."""

    def __init__(self, stream=None, audit: Optional[AuditLog] = None, source: str = "CLI") -> None:
        self._audit, self._source = audit, source
        self._out = stream or sys.stdout
        self._tty = self._out.isatty()
        self._lock = threading.Lock()
        self._on_progress_line = False
        self.failed = False

    def __call__(self, event: Event) -> None:
        with self._lock:
            if isinstance(event, LogEvent):
                if self._audit is not None:
                    self._audit.write(self._source, event)
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


def _audit_for(args, source: str) -> Optional[AuditLog]:
    if not getattr(args, "audit", False):
        return None
    from .platform.paths import data_dir, fix_owner

    folder = data_dir()
    return AuditLog(folder / "logs", fix_owner, key=load_or_create_key(folder / "audit.key", fix_owner))


def _bind_ip(args) -> str:
    """--ip wins; ``all`` = every IPv4 interface; --to DEVICE picks the NIC that routes to that device."""
    from .platform.network import default_route_ip, ip_for_target

    if args.ip and args.to:
        raise SystemExit("error: use either --ip or --to, not both")
    if args.to:
        ip = ip_for_target(args.to)
        if not ip:
            raise SystemExit(f"error: no route to {args.to} - cannot choose an interface (use --ip)")
        return ip
    if args.ip == "all":
        return "0.0.0.0"
    return args.ip or default_route_ip() or "0.0.0.0"


def cmd_serve(args) -> int:
    from .platform.detect import detect_platform
    from .platform.privileges import probe_udp_bind, remedies

    sink = ConsoleSink(audit=_audit_for(args, f"CLI serve {args.protocol}"), source=f"CLI serve {args.protocol}")
    ip = _bind_ip(args)
    policy = _policy(args)
    if (args.cert or args.key) and not (args.protocol == "http" and args.https):
        raise SystemExit("error: --cert/--key need 'serve http --https'")
    if args.key and not args.cert:
        raise SystemExit("error: --key needs --cert")
    if args.protocol == "tftp":
        if is_ipv6(ip):
            raise SystemExit("error: TFTP serves IPv4 only (the tftpy engine has no IPv6 support); use 'serve http'")
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
        service,
        lambda: service.serve(
            args.file, ip, args.port or HTTP_PORT, policy, args.https, args.cert or "", args.key or ""
        ),
        sink,
    )


def _usage(message: str) -> SystemExit:
    return SystemExit(f"error: {message}")


def _post_push_options(args):
    """Validate and build the verify / install options from the shared push flags."""
    protocol = args.protocol
    verify = None
    if args.verify or args.verify_cmd:
        if protocol in ("ftp", "ftps"):
            raise _usage("--verify needs scp or sftp (it runs a command on the device over SSH)")
        if not args.vendor and not args.verify_cmd:
            raise _usage("--verify needs --vendor (a known verify command) or --verify-cmd")
        bad = check_verify_template(args.verify_cmd) if args.verify_cmd else None
        if bad:
            raise _usage(f"--verify-cmd: {bad}")
        verify = VerifyOptions(args.verify_cmd or "", args.verify_algo or "", args.verify_timeout)
    install = None
    if args.install:
        if protocol in ("ftp", "ftps"):
            raise _usage("--install needs scp or sftp")
        if not args.vendor:
            raise _usage("--install needs --vendor")
        install = InstallOptions(args.execute, args.reload)
        if args.execute and verify is None:
            raise _usage(
                "--execute changes the boot image: add --verify so a corrupt upload can never become the boot image"
            )
    elif args.execute or args.reload:
        raise _usage("--execute/--reload only make sense with --install")
    if args.reload and not args.execute:
        raise _usage("--reload needs --execute (without it --install is only a dry run)")
    if args.preflight and protocol in ("ftp", "ftps"):
        raise _usage("--preflight needs scp or sftp")
    if args.resume and protocol == "scp":
        raise _usage("--resume needs sftp, ftp or ftps: the SCP protocol cannot continue a partial upload")
    if args.vendor:
        try:
            get_vendor(args.vendor)
        except UnknownVendorError as exc:
            raise _usage(str(exc)) from None
    return verify, install


def _jump(args):
    if not args.jump:
        return None
    if args.protocol in ("ftp", "ftps"):
        raise _usage("--jump needs scp or sftp (FTP opens a second data connection that cannot be tunnelled)")
    try:
        jump = parse_jump(args.jump)
    except ValueError as exc:
        raise _usage(f"--jump: {exc}") from None
    from dataclasses import replace

    password = _secret(args.jump_password_env, f"Password for jump host {jump.host}: ", not args.jump_key)
    passphrase = _secret(args.jump_key_passphrase_env, "", False)
    return replace(
        jump,
        username=jump.username or args.user or "",
        password=password,
        key_path=args.jump_key or "",
        key_passphrase=passphrase,
    )


def _defaults(args, password: str, passphrase: str) -> PushDefaults:
    verify, install = _post_push_options(args)
    credentials = None
    if args.use_keyring:
        credentials = CredentialStore().get
    return PushDefaults(
        args.protocol,
        args.user or "",
        password,
        args.key or "",
        passphrase,
        args.port,
        args.remote or "",
        args.accept_changed_key,
        vendor=args.vendor or "",
        preflight=args.preflight,
        verify=verify,
        install=install,
        resume=args.resume,
        jump=_jump(args),
        credentials=credentials,
    )


def _store() -> KnownHostsStore:
    from .platform.paths import data_dir, fix_owner

    return KnownHostsStore(data_dir() / "known_hosts", fix_owner)


def _needs_password(args) -> bool:
    return not args.key and not args.use_keyring


def cmd_push(args) -> int:
    sink = ConsoleSink(audit=_audit_for(args, "CLI push"), source="CLI push")
    password = _secret(args.password_env, f"Password for {args.user or 'device'}@{args.host}: ", _needs_password(args))
    passphrase = _secret(args.key_passphrase_env, "", False)
    defaults = _defaults(args, password, passphrase)
    service = make_service_factory(args.protocol, _store())(sink)
    request = defaults.build(args.host, args.file)
    return _run_blocking(service, lambda: service.push(request), sink, "cancel")


def cmd_batch(args) -> int:
    sink = ConsoleSink(audit=_audit_for(args, "CLI batch"), source="CLI batch")
    path = args.inventory or args.targets
    try:
        targets = load_inventory(path, args.inventory_format if args.inventory else "csv")
        if args.filter:
            targets = filter_targets(targets, args.filter)
            if not targets:
                print(f"error: --filter '{args.filter}' matches no device", file=sys.stderr)
                return USAGE
    except (BatchFileError, OSError) as exc:
        for line in getattr(exc, "errors", [str(exc)]):
            print(f"error: {path}: {line}", file=sys.stderr)
        return USAGE
    if args.list_targets:
        for t in targets:
            print(f"{t.host:<24} {t.vendor or '-':<10} {' '.join(t.tags)}")
        print(f"{len(targets)} device(s)")
        return OK
    password = _secret(args.password_env, "Default password for devices without their own: ", _needs_password(args))
    passphrase = _secret(args.key_passphrase_env, "", False)
    defaults = _defaults(args, password, passphrase)
    runner = BatchRunner(
        sink,
        make_service_factory(args.protocol, _store()),
        lambda t: defaults.build(t.host, args.file, t),
        args.workers,
        retries=args.retries,
        backoff=args.backoff,
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


def cmd_interfaces(args) -> int:
    from .platform.network import interface_for_target, list_interfaces

    found = list_interfaces(include_ipv6=args.ipv6)
    for i in found:
        print(i.label)
    if args.to:
        chosen = interface_for_target(args.to, found)
        print(f"\nroute to {args.to}: " + (chosen.label if chosen else "no matching interface (no route?)"))
    return OK if found else FAILED


def cmd_credentials(args) -> int:
    store = CredentialStore()
    try:
        if args.action == "set":
            password = _secret(
                args.password_env, f"Password for {args.user}@{args.host} (stored in the OS keyring): ", True
            )
            if not password:
                print("error: no password given", file=sys.stderr)
                return USAGE
            store.set(args.host, args.user, password)
            print(f"Stored credential for {args.user}@{args.host} in the OS keyring.")
        else:
            print("Deleted." if store.delete(args.host, args.user) else "Nothing stored for that device/user.")
    except (KeyringUnavailableError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return FAILED
    return OK


def cmd_audit(args) -> int:
    from pathlib import Path

    from .platform.paths import data_dir

    folder = data_dir()
    key_file = Path(args.key_file) if args.key_file else folder / "audit.key"
    key = key_file.read_bytes() if key_file.is_file() else None
    files = [Path(f) for f in args.files] or sorted((folder / "logs").glob("nettransfer-*.log"))
    if not files:
        print("no audit logs found")
        return FAILED
    worst = OK
    for path in files:
        report = verify_chain(path, key)
        status = "OK" if report.ok else "TAMPERED"
        extra = f", {report.legacy} unchained legacy line(s)" if report.legacy else ""
        print(
            f"[{status:<8}] {path.name}: {report.chained} chained line(s){extra}"
            + (f"; head {report.head}" if report.head else "")
        )
        if not report.ok:
            print(f"           {report.problem}")
            worst = FAILED
        if args.expect_head and report.head and report.head != args.expect_head:
            print("           head differs from --expect-head: the file was truncated or replaced")
            worst = FAILED
    return worst


def cmd_history(args) -> int:
    from pathlib import Path

    from .platform.paths import data_dir

    folder = Path(args.dir) if args.dir else data_dir() / "logs"
    history = load_history(folder).filtered(args.device or "", args.file or "")
    if args.html:
        Path(args.html).write_text(render_html(history), encoding="utf-8")
        print(f"Report written to {args.html} ({len(history.records)} transfers)")
    else:
        print(text_summary(history))
    return OK


def cmd_web(args) -> int:
    from . import webui
    from .platform.network import list_interfaces

    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token:
        raise _usage(f"environment variable {args.token_env} is not set")
    generated = token is None
    token = token or webui.new_token()
    try:
        server, controller = webui.serve_web(
            args.files, args.bind, args.port, token, lambda: list_interfaces(), args.https
        )
    except (ValueError, OSError) as exc:
        raise _usage(str(exc)) from None
    scheme = "https" if args.https else "http"
    host, port = server.server_address[:2]
    print(f"NetTransfer web panel on {scheme}://{host}:{port}/")
    if generated:
        print(f"Access token (shown once): {token}")
    if not webui.is_loopback(args.bind) and not args.https:
        print("WARNING: bound beyond loopback without --https: the token travels in clear text", file=sys.stderr)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("\nStopping ...", file=sys.stderr)
    controller.shutdown()
    server.shutdown()
    server.server_close()
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nettransfer", description="Send files to network devices (headless mode).")
    parser.add_argument("--version", action="version", version=f"nettransfer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve a file for devices to pull (TFTP or HTTP/HTTPS)")
    serve.add_argument("protocol", choices=["tftp", "http"])
    serve.add_argument("file")
    serve.add_argument(
        "--ip", help="local IP to bind: an address, 'all' (every IPv4 interface) or an IPv6 address (http only)"
    )
    serve.add_argument(
        "--to", metavar="DEVICE_IP", help="bind the interface that routes to this device (instead of --ip)"
    )
    serve.add_argument("--port", type=int, help=f"default {TFTP_PORT} (tftp) / {HTTP_PORT} (http)")
    serve.add_argument("--https", action="store_true", help="http only: TLS (self-signed unless --cert is given)")
    serve.add_argument("--cert", metavar="PEM", help="https: serve this certificate instead of a throw-away one")
    serve.add_argument("--key", metavar="PEM", help="https: its private key (omit if the key is in the --cert file)")
    serve.add_argument("--audit", action="store_true", help="also write the hash-chained audit log")
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
        p.add_argument(
            "--use-keyring", action="store_true", help="look up the password in the OS keyring (see 'credentials')"
        )
        p.add_argument(
            "--jump", metavar="[USER@]HOST[:PORT]", help="reach the device through this SSH bastion (scp/sftp)"
        )
        p.add_argument("--jump-key", metavar="PATH", help="SSH private key for the jump host")
        p.add_argument("--jump-password-env", metavar="VAR")
        p.add_argument("--jump-key-passphrase-env", metavar="VAR")
        p.add_argument(
            "--vendor",
            metavar="ID",
            help="cisco-ios, cisco-nxos, junos, huawei, fortinet, linux (enables the options below)",
        )
        p.add_argument(
            "--preflight", action="store_true", help="check the image name and free space on the device before sending"
        )
        p.add_argument(
            "--verify", action="store_true", help="after the push, compare the device's digest with the local one"
        )
        p.add_argument(
            "--verify-cmd", metavar="TEMPLATE", help="custom verify command, e.g. 'sha256sum {path}' ({path} {file})"
        )
        p.add_argument("--verify-algo", choices=["md5", "sha1", "sha256", "sha512"])
        p.add_argument("--verify-timeout", type=float, default=900.0, metavar="SECONDS")
        p.add_argument(
            "--install", action="store_true", help="set the boot image after a verified push (dry run unless --execute)"
        )
        p.add_argument("--execute", action="store_true", help="really apply the --install plan on the device")
        p.add_argument("--reload", action="store_true", help="with --execute: also reload the device")
        p.add_argument("--resume", action="store_true", help="continue a partial upload (sftp, ftp, ftps)")
        p.add_argument("--audit", action="store_true", help="also write the hash-chained audit log")

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
    source = batch.add_mutually_exclusive_group(required=True)
    source.add_argument("--targets", help="CSV with columns host[,username,password,remote_path,port,vendor,tags]")
    source.add_argument("--inventory", help="CSV, Ansible YAML, Netmiko YAML/JSON or Zabbix JSON export")
    batch.add_argument("--inventory-format", default="auto", choices=["auto", "csv", "ansible", "netmiko", "zabbix"])
    batch.add_argument(
        "--filter", metavar="EXPR", help="only matching devices, e.g. 'site:DCTT vendor:fortinet,juniper !group:lab'"
    )
    batch.add_argument("--list-targets", action="store_true", help="print the selected devices and exit")
    batch.add_argument(
        "--retries", type=int, default=0, help="retry transient failures (timeouts, resets) this many times"
    )
    batch.add_argument(
        "--backoff", type=float, default=2.0, metavar="SECONDS", help="first retry delay; doubles each time"
    )
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

    interfaces = sub.add_parser("interfaces", help="list local addresses; --to shows which one reaches a device")
    interfaces.add_argument("--ipv6", action="store_true", help="include global/ULA IPv6 addresses")
    interfaces.add_argument("--to", metavar="DEVICE_IP")
    interfaces.set_defaults(func=cmd_interfaces)

    creds = sub.add_parser("credentials", help="store/delete a device password in the OS keyring")
    creds.add_argument("action", choices=["set", "delete"])
    creds.add_argument("host", help="device address, or * as the fallback for every device")
    creds.add_argument("user")
    creds.add_argument("--password-env", metavar="VAR")
    creds.set_defaults(func=cmd_credentials)

    audit = sub.add_parser("audit", help="verify the hash chain of the audit logs (tamper detection)")
    audit.add_argument("files", nargs="*", help="log files (default: all in the data folder)")
    audit.add_argument("--key-file", metavar="PATH", help="HMAC key (default: audit.key in the data folder)")
    audit.add_argument("--expect-head", metavar="TAG", help="the head you recorded in a ticket; detects a cut-off tail")
    audit.set_defaults(func=cmd_audit)

    history = sub.add_parser("history", help="which device got which file, when (from the audit logs)")
    history.add_argument("--dir", help="folder with nettransfer-*.log (default: the data folder's logs)")
    history.add_argument("--device", help="only devices containing this text")
    history.add_argument("--file", help="only files containing this text")
    history.add_argument("--html", metavar="OUT", help="write a self-contained HTML report instead of a text summary")
    history.set_defaults(func=cmd_history)

    web = sub.add_parser("web", help="browser control panel for a shared lab server (token protected)")
    web.add_argument("--files", required=True, metavar="DIR", help="the only folder whose files can be served")
    web.add_argument("--bind", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--https", action="store_true", help="TLS with a throw-away self-signed certificate")
    web.add_argument(
        "--token-env", metavar="VAR", help="read the access token from this variable (default: generate one)"
    )
    web.set_defaults(func=cmd_web)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
