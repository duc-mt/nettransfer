"""Web control panel for a shared lab server: start/stop TFTP and HTTP(S) servers from a browser.

Deliberately small and defensive (Qt-free, standard library only):

* a **token is mandatory** and travels in an ``Authorization: Bearer`` header (never a cookie, so no CSRF) and is
  compared in constant time; repeated failures are slowed down and then locked out;
* it only ever serves files that sit directly inside the configured ``--files`` directory and are picked from the
  listing by exact name - there are no uploads and no path parameters, so nothing can be traversed;
* it binds to loopback unless told otherwise, can use HTTPS (``--https``) and answers with a strict CSP;
* every string shown comes from device-controlled text (a TFTP client chooses the file name it asks for), so the
  page renders everything with ``textContent`` - never ``innerHTML``.
"""

from __future__ import annotations

import hmac
import json
import secrets
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .core.events import Event, LogEvent, State, StateEvent
from .core.http_service import HttpService, _Server, _Server6, is_ipv6
from .core.serve_policy import ServePolicy, parse_allowlist
from .core.tftp_service import DEFAULT_PORT as TFTP_PORT
from .core.tftp_service import TftpService
from .platform.network import Interface

MAX_BODY = 4096
MAX_JOBS = 8
MIN_TOKEN = 16
LOG_LINES = 300


class WebError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    id: str
    protocol: str
    file: str
    ip: str
    port: int
    state: str = "starting"  # starting | running | stopped | failed
    detail: str = ""
    started: float = field(default_factory=time.time)
    log: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    service: Any = None
    thread: Optional[threading.Thread] = None


class WebController:
    """The state behind the page; testable without HTTP."""

    def __init__(
        self,
        files_dir: Path,
        interfaces: Callable[[], List[Interface]],
        tftp_factory: Callable[..., Any] = TftpService,
        http_factory: Callable[..., Any] = HttpService,
    ) -> None:
        self._dir = Path(files_dir).resolve()
        self._interfaces = interfaces
        self._factories = {"tftp": tftp_factory, "http": http_factory, "https": http_factory}
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}

    # -- read side ---------------------------------------------------------
    def files(self) -> List[Dict[str, Any]]:
        out = []
        try:
            entries = sorted(self._dir.iterdir())
        except OSError:
            return []
        for p in entries:
            if p.name.startswith(".") or p.is_symlink() or not p.is_file():
                continue
            out.append({"name": p.name, "size": p.stat().st_size})
        return out

    def addresses(self) -> List[str]:
        ips = ["0.0.0.0"] + [i.ip for i in self._interfaces()]
        return list(dict.fromkeys(ips))

    def state(self) -> Dict[str, Any]:
        with self._lock:
            jobs = [
                {
                    "id": j.id,
                    "protocol": j.protocol,
                    "file": j.file,
                    "ip": j.ip,
                    "port": j.port,
                    "state": j.state,
                    "detail": j.detail,
                    "log": list(j.log),
                }
                for j in self._jobs.values()
            ]
        return {"files": self.files(), "addresses": self.addresses(), "jobs": jobs}

    # -- write side --------------------------------------------------------
    def start(self, request: Dict[str, Any]) -> str:
        protocol = str(request.get("protocol", ""))
        if protocol not in self._factories:
            raise WebError("protocol must be tftp, http or https")
        name = str(request.get("file", ""))
        if name not in {f["name"] for f in self.files()}:  # exact match against the listing: no traversal possible
            raise WebError("unknown file")
        ip = str(request.get("ip", "0.0.0.0"))
        if ip not in self.addresses():
            raise WebError("unknown address: pick one from the list")
        try:
            port = int(request.get("port") or (TFTP_PORT if protocol == "tftp" else 8080))
        except (TypeError, ValueError):
            raise WebError("invalid port") from None
        if not 1 <= port <= 65535:
            raise WebError("invalid port")
        try:
            allowed = parse_allowlist(str(request.get("allow", "")))
            idle = float(request.get("idle") or 0)
        except ValueError as exc:
            raise WebError(str(exc)) from None
        if idle < 0:
            raise WebError("idle timeout must not be negative")
        policy = ServePolicy(bool(request.get("once")), idle or None, allowed)
        with self._lock:
            if sum(1 for j in self._jobs.values() if j.state in ("starting", "running")) >= MAX_JOBS:
                raise WebError("too many running servers", 409)
            if any(
                j.state in ("starting", "running") and j.port == port and j.protocol == protocol
                for j in self._jobs.values()
            ):
                raise WebError(f"a {protocol} server is already running on port {port}", 409)
            job = Job(secrets.token_hex(4), protocol, name, ip, port)
            self._jobs[job.id] = job
        service = self._factories[protocol](lambda event, job=job: self._on_event(job, event))
        job.service = service
        path = str(self._dir / name)
        if protocol == "tftp":
            target: Callable[[], None] = lambda: service.serve(path, ip, port, policy)  # noqa: E731
        else:
            target = lambda: service.serve(path, ip, port, policy, protocol == "https")  # noqa: E731
        job.thread = threading.Thread(target=target, daemon=True, name=f"web-{protocol}-{job.id}")
        job.thread.start()
        return job.id

    def stop(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise WebError("unknown server", 404)
        if job.service is not None:
            job.service.stop()

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            if job.service is not None and job.state in ("starting", "running"):
                job.service.stop()

    def _on_event(self, job: Job, event: Event) -> None:
        if isinstance(event, LogEvent):
            job.log.append(
                f"{time.strftime('%H:%M:%S', time.localtime(event.ts))} {event.level.value.upper():<7} {event.message}"
            )
        elif isinstance(event, StateEvent):
            if event.state is State.STARTED:
                job.state = "running"
            elif event.state is State.FAILED:
                job.state, job.detail = "failed", event.detail
            elif event.state is State.STOPPED:
                job.state = "stopped" if job.state != "failed" else job.state


class _Throttle:
    """Slow down and then lock out token guessing (per client address)."""

    def __init__(self, limit: int = 10, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit, self._window, self._clock = limit, window, clock
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def locked(self, client: str) -> bool:
        now = self._clock()
        with self._lock:
            recent = [t for t in self._fails.get(client, []) if now - t < self._window]
            self._fails[client] = recent
            return len(recent) >= self._limit

    def failed(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(self._clock())


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NetTransfer</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:1.5rem auto;max-width:60rem;padding:0 1rem}
fieldset{margin:1rem 0;border:1px solid #8886;border-radius:6px}
label{display:inline-block;margin:.25rem .75rem .25rem 0}
pre{background:#8882;padding:.5rem;max-height:12rem;overflow:auto;font-size:.8rem;white-space:pre-wrap;word-break:break-all}
.job{border:1px solid #8886;border-radius:6px;padding:.5rem .75rem;margin:.75rem 0}
.running{border-color:#2da44e}.failed{border-color:#cf222e}
#err{color:#cf222e}
</style></head><body>
<h1>NetTransfer</h1>
<div id="login"><label>Access token <input id="token" type="password" autocomplete="off"></label>
<button id="go">Connect</button></div>
<p id="err" role="alert"></p>
<div id="app" hidden>
<fieldset><legend>Start a server</legend>
<label>Protocol <select id="protocol"><option>tftp</option><option>http</option><option>https</option></select></label>
<label>File <select id="file"></select></label>
<label>Address <select id="ip"></select></label>
<label>Port <input id="port" size="5" placeholder="default"></label><br>
<label><input id="once" type="checkbox"> stop after the first transfer</label>
<label>Idle stop (s) <input id="idle" size="5"></label>
<label>Allow only <input id="allow" size="24" placeholder="10.0.0.0/24,192.168.1.5"></label>
<button id="start">Start</button></fieldset>
<div id="jobs"></div></div>
<script nonce="__NONCE__">
let token = sessionStorage.getItem("nt-token") || "";
const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const r = await fetch(path, {method: body ? "POST" : "GET", headers: {"Authorization": "Bearer " + token,
    "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
};
const fill = (sel, values, label) => { const keep = sel.value; sel.replaceChildren(...values.map((v) => {
  const o = document.createElement("option"); o.value = v; o.textContent = label ? label(v) : v; return o; }));
  if (values.includes(keep)) sel.value = keep; };
async function refresh() {
  try {
    const s = await api("/api/state"); $("err").textContent = "";
    $("login").hidden = true; $("app").hidden = false;
    fill($("file"), s.files.map((f) => f.name)); fill($("ip"), s.addresses);
    const box = $("jobs"); box.replaceChildren();
    for (const j of s.jobs) {
      const d = document.createElement("div"); d.className = "job " + j.state;
      const h = document.createElement("strong");
      h.textContent = j.protocol.toUpperCase() + " " + j.ip + ":" + j.port + " - " + j.file + " [" + j.state + "]";
      d.append(h);
      if (j.state === "running" || j.state === "starting") {
        const b = document.createElement("button"); b.textContent = "Stop"; b.style.marginLeft = ".75rem";
        b.onclick = () => api("/api/stop", {id: j.id}).then(refresh, (e) => $("err").textContent = e.message);
        d.append(b);
      }
      const pre = document.createElement("pre"); pre.textContent = j.log.join("\\n"); d.append(pre); box.append(d);
    }
  } catch (e) { $("err").textContent = e.message; if (!$("app").hidden) return; sessionStorage.removeItem("nt-token"); }
}
$("go").onclick = () => { token = $("token").value; sessionStorage.setItem("nt-token", token); refresh(); };
$("start").onclick = () => api("/api/start", {protocol: $("protocol").value, file: $("file").value, ip: $("ip").value,
  port: $("port").value, once: $("once").checked, idle: $("idle").value, allow: $("allow").value})
  .then(refresh, (e) => $("err").textContent = e.message);
if (token) refresh();
setInterval(() => { if (!$("app").hidden) refresh(); }, 2000);
</script></body></html>
"""


def make_handler(controller: WebController, token: str, throttle: Optional[_Throttle] = None) -> type:
    guard = throttle or _Throttle()

    class Handler(BaseHTTPRequestHandler):
        server_version = "NetTransferWeb"
        timeout = 30  # a stalled client must not hold a thread forever

        def setup(self) -> None:
            if isinstance(self.request, ssl.SSLSocket):  # handshake here, not in accept(): one slow client cannot block
                self.request.settimeout(15)
                self.request.do_handshake()
            super().setup()

        def log_message(self, *_args: object) -> None:
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _authorized(self) -> bool:
            client = self.client_address[0]
            if guard.locked(client):
                self._json(429, {"error": "too many failed attempts - wait a minute"})
                return False
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if hmac.compare_digest(supplied.encode(), token.encode()):
                return True
            guard.failed(client)
            time.sleep(0.4)  # make guessing slow even before the lockout
            self._json(401, {"error": "wrong or missing token"})
            return False

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/":
                nonce = secrets.token_urlsafe(12)
                csp = (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                )
                page = PAGE.replace("__NONCE__", nonce).encode()
                self._send(
                    200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"}
                )
            elif path == "/api/state":
                if self._authorized():
                    self._json(200, controller.state())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            if path not in ("/api/start", "/api/stop"):
                self._json(404, {"error": "not found"})
                return
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise WebError("request body missing or too large", 413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise WebError("expected a JSON object")
                if path == "/api/start":
                    self._json(200, {"id": controller.start(body)})
                else:
                    controller.stop(str(body.get("id", "")))
                    self._json(200, {"ok": True})
            except WebError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "invalid JSON"})

    return Handler


def new_token() -> str:
    return secrets.token_urlsafe(24)


def validate_token(token: str) -> str:
    if len(token) < MIN_TOKEN:
        raise ValueError(f"the access token must be at least {MIN_TOKEN} characters")
    return token


def serve_web(
    files_dir: Path,
    bind: str,
    port: int,
    token: str,
    interfaces: Callable[[], List[Interface]],
    https: bool = False,
    ready: Optional[Callable[[Tuple[str, int]], None]] = None,
) -> Tuple[_Server, WebController]:
    """Create (but do not run) the server; the caller owns ``serve_forever``/``shutdown``."""
    if not Path(files_dir).is_dir():
        raise ValueError(f"--files: '{files_dir}' is not a directory")
    controller = WebController(Path(files_dir), interfaces)
    # the shared server classes bind without a reverse-DNS lookup (a stall there froze start-up on slow resolvers)
    server = (_Server6 if is_ipv6(bind) else _Server)((bind, port), make_handler(controller, validate_token(token)))
    if https:
        from .core.http_service import _tls_context

        context, _fp, _self_signed = _tls_context(bind)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    if ready:
        ready((bind, int(server.server_address[1])))
    return server, controller


def is_loopback(address: str) -> bool:
    return address in ("127.0.0.1", "::1", "localhost") or address.startswith("127.")


__all__ = ["WebController", "WebError", "is_loopback", "make_handler", "new_token", "serve_web", "validate_token"]
