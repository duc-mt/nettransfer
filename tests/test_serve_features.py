import hashlib
import http.client
import io
import os
import socket
import ssl
import threading
import time

import pytest
import tftpy

from app.core.audit import AuditLog
from app.core.device_hints import HTTP_HINTS, REMOTE_PATH_PRESETS, TFTP_HINTS, render
from app.core.events import Level, LogEvent, State, StateEvent
from app.core.hashing import HashService, file_digests
from app.core.http_service import HttpService, make_self_signed
from app.core.serve_policy import ServeGuard, ServePolicy, parse_allowlist
from app.core.settings import SettingsStore
from app.core.tftp_service import TftpService


def free_port(kind=socket.SOCK_DGRAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def started(events):
    return any(isinstance(e, StateEvent) and e.state is State.STARTED for e in events)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def start(service, *args, **kwargs):
    t = threading.Thread(target=lambda: service.serve(*args, **kwargs), daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- policy
def test_parse_allowlist():
    nets = parse_allowlist("10.0.0.0/24, 192.168.1.5  ;172.16.0.0/12")
    assert len(nets) == 3
    assert parse_allowlist("") == ()
    with pytest.raises(ValueError, match="not-an-ip"):
        parse_allowlist("10.0.0.1, not-an-ip")


def test_guard_allowlist_and_idle():
    events, stops = [], []
    guard = ServeGuard(ServePolicy(allowed=parse_allowlist("10.1.0.0/16")), events.append, lambda: stops.append(1))
    assert guard.client_allowed("10.1.2.3")
    assert not guard.client_allowed("10.2.0.1")
    assert not guard.client_allowed(None)
    assert any("blocked" in e.message for e in events)
    assert ServeGuard(ServePolicy(), events.append, lambda: None).client_allowed("8.8.8.8")


def test_guard_idle_timeout_stops_once():
    stops, done = [], threading.Event()
    guard = ServeGuard(ServePolicy(idle_timeout=0.3), lambda e: None, lambda: stops.append(1))
    guard.start_watchdog(done, interval=0.05)
    assert wait_for(lambda: stops, 3)
    done.set()
    time.sleep(0.2)
    assert stops == [1]


def test_guard_idle_not_triggered_while_transferring():
    stops, done = [], threading.Event()
    guard = ServeGuard(ServePolicy(idle_timeout=0.2), lambda e: None, lambda: stops.append(1))
    guard.transfer_started()
    guard.start_watchdog(done, interval=0.05)
    time.sleep(0.6)
    done.set()
    assert stops == []


# ---------------------------------------------------------------- TFTP policy
@pytest.fixture
def tftp(tmp_path):
    payload = os.urandom(30_000)
    src = tmp_path / "fw.bin"
    src.write_bytes(payload)
    events, port = [], free_port()
    svc = TftpService(events.append, select_timeout=0.4)
    threads = []

    def launch(policy):
        threads.append(start(svc, str(src), "127.0.0.1", port, policy))
        assert wait_for(lambda: started(events))
        return payload, port, events, threads[-1]

    yield launch
    svc.stop()
    for t in threads:
        t.join(5)


def test_tftp_serve_once_stops_after_first_success(tftp, tmp_path):
    payload, port, events, thread = tftp(ServePolicy(stop_after_first=True))
    out = tmp_path / "o.bin"
    tftpy.TftpClient("127.0.0.1", port).download("a", str(out))
    assert out.read_bytes() == payload
    thread.join(5)
    assert not thread.is_alive()
    assert any(isinstance(e, LogEvent) and "Serve-once" in e.message for e in events)
    assert events[-1] == StateEvent(State.STOPPED)


def test_tftp_allowlist_blocks_other_clients(tftp):
    _, port, events, _ = tftp(ServePolicy(allowed=parse_allowlist("10.99.0.0/16")))
    buf = io.BytesIO()
    with pytest.raises(tftpy.TftpException):
        tftpy.TftpClient("127.0.0.1", port).download("a", buf)
    assert wait_for(lambda: any(isinstance(e, LogEvent) and "blocked" in e.message for e in events))


def test_tftp_idle_timeout_stops_server(tftp):
    _, _, events, thread = tftp(ServePolicy(idle_timeout=0.6))
    thread.join(6)
    assert not thread.is_alive()
    assert any(isinstance(e, LogEvent) and "Idle for" in e.message for e in events)


def test_tftp_logs_block_size(tftp, tmp_path):
    _, port, events, _ = tftp(ServePolicy())
    tftpy.TftpClient("127.0.0.1", port, options={"blksize": 1428}).download("a", str(tmp_path / "o.bin"))
    assert wait_for(lambda: any(isinstance(e, LogEvent) and "block size 1428" in e.message for e in events))
    assert not any(isinstance(e, LogEvent) and "512-byte blocks" in e.message for e in events)


# ---------------------------------------------------------------- HTTP(S)
@pytest.fixture
def httpd(tmp_path):
    payload = os.urandom(300_000)
    src = tmp_path / "image.bin"
    src.write_bytes(payload)
    svc, threads = None, []

    def launch(policy=ServePolicy(), https=False):
        nonlocal svc
        events = []
        svc = HttpService(events.append)
        threads.append(start(svc, str(src), "127.0.0.1", 0, policy, https))
        assert wait_for(lambda: started(events), timeout=10.0), f"HTTP server failed to start: {events}"
        ev = next(e for e in events if isinstance(e, StateEvent) and e.state is State.STARTED)
        port = int(ev.detail.rsplit(":", 1)[1])
        return payload, port, events, threads[-1]

    yield launch
    if svc:
        svc.stop()
    for t in threads:
        t.join(5)


def _get(port, path="/x.img", method="GET", tls=False):
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_http_serves_file_for_any_path(httpd):
    payload, port, events, _ = httpd()
    resp, body = _get(port, "/anything/else.bin")
    assert resp.status == 200 and body == payload
    assert resp.getheader("Content-Length") == str(len(payload))
    assert wait_for(
        lambda: any(isinstance(e, LogEvent) and e.level is Level.SUCCESS and "sent" in e.message for e in events)
    )


def test_http_head_and_readonly(httpd):
    payload, port, _, _ = httpd()
    resp, body = _get(port, method="HEAD")
    assert resp.status == 200 and body == b"" and resp.getheader("Content-Length") == str(len(payload))
    resp, _ = _get(port, method="PUT")
    assert resp.status in (501, 405)


def test_http_allowlist_and_once(httpd):
    _, port, events, _ = httpd(ServePolicy(allowed=parse_allowlist("10.99.0.0/16")))
    resp, _ = _get(port)
    assert resp.status == 403
    assert any(isinstance(e, LogEvent) and "blocked" in e.message for e in events)


def test_http_serve_once_stops(httpd):
    payload, port, events, thread = httpd(ServePolicy(stop_after_first=True))
    _, body = _get(port)
    assert body == payload
    thread.join(5)
    assert not thread.is_alive() and events[-1] == StateEvent(State.STOPPED)


def test_https_with_self_signed_cert(httpd):
    payload, port, events, _ = httpd(https=True)
    resp, body = _get(port, tls=True)
    assert resp.status == 200 and body == payload
    assert any(isinstance(e, LogEvent) and "Self-signed" in e.message for e in events)


def test_http_port_in_use_fails_cleanly(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    events = []
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        HttpService(events.append).serve(str(src), "127.0.0.1", blocker.getsockname()[1])
    assert events[-1].state is State.FAILED and "already in use" in events[-1].detail


def test_self_signed_cert_has_ip_san():
    from cryptography import x509

    cert_pem, key_pem = make_self_signed("192.0.2.10")
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.0.2.10" in [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    assert b"PRIVATE KEY" in key_pem


# ---------------------------------------------------------------- misc core
def test_file_digests_and_service(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world")
    d = file_digests(str(f))
    assert d["md5"] == hashlib.md5(b"hello world").hexdigest()
    assert d["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    events = []
    HashService(events.append).run(str(f))
    assert any(d["sha256"] in e.message for e in events)
    events.clear()
    HashService(events.append).run(str(tmp_path / "missing"))
    assert events[0].level is Level.ERROR


def test_settings_roundtrip_and_corruption(tmp_path):
    path = tmp_path / "sub" / "settings.json"
    s = SettingsStore(path)
    s.set("a.b", 5)
    s.save()
    assert SettingsStore(path).get("a.b") == 5 and SettingsStore(path).get("zzz", "d") == "d"
    path.write_text("{not json")
    assert SettingsStore(path).get("a.b") is None  # corrupt -> defaults, no crash
    assert SettingsStore(None).get("x") is None


def test_audit_log_writes_without_newlines(tmp_path):
    audit = AuditLog(tmp_path / "logs")
    audit.write("TFTP", LogEvent(Level.SUCCESS, "line one\nline two"))
    text = audit.path.read_text()
    assert "[TFTP] SUCCESS" in text and "line one line two" in text and text.count("\n") == 1
    audit.enabled = False
    audit.write("TFTP", LogEvent(Level.INFO, "ignored"))
    assert "ignored" not in audit.path.read_text()


def test_hint_templates_render():
    cisco = TFTP_HINTS[0]
    assert render(cisco.pull, ip="10.0.0.5", file="fw.bin") == "copy tftp://10.0.0.5/fw.bin flash:fw.bin"
    junos = [h for h in HTTP_HINTS if "Junos" in h.vendor][0]
    assert render(junos.pull, scheme="https", ip="10.0.0.5", port=8443, file="x.tgz").startswith(
        "file copy https://10.0.0.5:8443/x.tgz"
    )
    assert REMOTE_PATH_PRESETS["Cisco IOS / IOS-XE"] == "flash:"


def test_data_dir_is_per_user(monkeypatch, tmp_path):
    from app.platform import paths

    monkeypatch.setattr(paths, "user_home", lambda: tmp_path)
    monkeypatch.setattr(paths, "_sudo_user", lambda: None)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    assert str(paths.data_dir()).startswith(str(tmp_path))
    target = tmp_path / "x" / "y"
    assert paths.ensure_dir(target).is_dir()
