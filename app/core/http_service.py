"""HTTP(S) file server: devices *pull* the selected file (copy http://... on Cisco/Junos).

Like the TFTP server it serves the selected file for any requested path and is read-only.
HTTPS uses a throw-away self-signed certificate (the device must be told not to verify it).
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import socketserver
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from .events import EventSink, Level, LogEvent, State, StateEvent
from .formatting import human_size, human_speed
from .progress import ProgressTracker
from .serve_policy import ServeGuard, ServePolicy
from .tftp_service import describe_bind_error

DEFAULT_PORT = 8080
_CHUNK = 64 * 1024


def make_self_signed(ip: str) -> Tuple[bytes, bytes]:
    """Return (certificate PEM, private key PEM) valid for 1 year for ``ip`` and localhost."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # RSA: widest device support
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NetTransfer")])
    san = [x509.DNSName("localhost")]
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        ),
    )


def _tls_context(ip: str) -> ssl.SSLContext:
    cert_pem, key_pem = make_self_signed(ip)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory(prefix="nettransfer-tls-") as tmp:  # load_cert_chain needs files
        cert_file, key_file = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with open(os.open(cert_file, flags, 0o600), "wb") as fh:
            fh.write(cert_pem)
        with open(os.open(key_file, flags, 0o600), "wb") as fh:
            fh.write(key_pem)
        context.load_cert_chain(cert_file, key_file)
    return context


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR lets a *second* server bind a port that is already listening on Windows (silent port
    # hijack), whereas on POSIX it only permits restarting through TIME_WAIT. Keep it for POSIX only.
    allow_reuse_address = os.name != "nt"
    tls_context: Optional[ssl.SSLContext] = None

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(host): a reverse-DNS lookup that can stall for tens of
        # seconds on hosts with slow resolvers (e.g. macOS CI runners) before the server ever starts. We never
        # use server_name, so bind without the lookup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls_context is not None:
            # handshake happens in the handler thread so one slow client cannot block accept()
            sock = self.tls_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def handle_error(self, request, client_address) -> None:
        pass


class HttpService:
    """Serve one file over HTTP(S). ``serve`` blocks: run it on a worker thread."""

    def __init__(self, on_event: EventSink) -> None:
        self._emit = on_event
        self._server: Optional[_Server] = None

    def serve(
        self,
        file_path: str,
        bind_ip: str,
        port: int = DEFAULT_PORT,
        policy: ServePolicy = ServePolicy(),
        https: bool = False,
    ) -> None:
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            self._fail(f"File not found: {path}")
            return
        name, size = os.path.basename(path), os.path.getsize(path)
        scheme = "https" if https else "http"
        guard = ServeGuard(policy, self._emit, self.stop)
        emit = self._emit

        class Handler(BaseHTTPRequestHandler):
            server_version = "NetTransfer"
            timeout = 60

            def setup(self) -> None:
                if isinstance(self.request, ssl.SSLSocket):
                    self.request.settimeout(15)
                    self.request.do_handshake()
                super().setup()

            def log_message(self, *_args) -> None:
                pass

            def _headers(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()

            def _allowed(self) -> bool:
                if guard.client_allowed(self.client_address[0]):
                    return True
                self.send_error(403, "Forbidden")
                return False

            def do_HEAD(self) -> None:
                if self._allowed():
                    self._headers()

            def do_GET(self) -> None:
                if not self._allowed():
                    return
                peer = self.client_address[0]
                emit(LogEvent(Level.INFO, f"{peer}: GET {self.path} -> serving {name} ({human_size(size)})"))
                guard.transfer_started()
                tracker, sent, ok = ProgressTracker(size, emit, peer), 0, False
                try:
                    self._headers()
                    with open(path, "rb") as fh:
                        while True:
                            chunk = fh.read(_CHUNK)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                            tracker.update(sent)
                    tracker.finish()
                    ok = True
                    emit(
                        LogEvent(
                            Level.SUCCESS,
                            f"{peer}: sent {human_size(size)} ({human_speed(tracker.average_bps)} avg)",
                            tag="transfer",
                        )
                    )
                except (OSError, ssl.SSLError):
                    pct = sent * 100 // size if size else 0
                    emit(
                        LogEvent(
                            Level.WARNING,
                            f"{peer}: transfer aborted at {pct}% ({human_size(sent)} of {human_size(size)})",
                        )
                    )
                finally:
                    guard.transfer_finished(ok)

        try:
            context = _tls_context(bind_ip) if https else None
            server = _Server((bind_ip, port), Handler)
        except OSError as exc:
            self._fail(describe_bind_error(exc, bind_ip, port))
            return
        server.tls_context = context
        self._server = server
        actual_port = server.server_address[1]
        done = threading.Event()
        guard.start_watchdog(done)
        addr = f"{scheme}://{bind_ip}:{actual_port}"
        self._emit(
            LogEvent(
                Level.SUCCESS,
                f"{scheme.upper()} server listening on {addr}/ - serving {name} ({human_size(size)})",
            )
        )
        if https:
            self._emit(LogEvent(Level.INFO, "Self-signed certificate: the device must skip certificate verification"))
        self._emit(StateEvent(State.STARTED, addr))
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            done.set()
            server.server_close()
            self._server = None
        self._emit(LogEvent(Level.INFO, f"{scheme.upper()} server stopped"))
        self._emit(StateEvent(State.STOPPED))

    def stop(self) -> None:
        server = self._server
        if server is not None:
            # shutdown() blocks until the loop exits; never call it on the serving thread itself
            threading.Thread(target=server.shutdown, daemon=True).start()

    def _fail(self, message: str) -> None:
        self._emit(LogEvent(Level.ERROR, message))
        self._emit(StateEvent(State.FAILED, message))
