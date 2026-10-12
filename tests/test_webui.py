"""Web control panel: auth, input validation, lifecycle, and the properties that make it safe to expose."""

import http.client
import json
import os
import ssl
import threading
import time

import pytest

from app import webui
from app.core.events import Level, LogEvent, State, StateEvent
from app.platform.network import Interface

TOKEN = "t" * 24


class FakeService:
    """Stands in for TftpService/HttpService: records the call, reports STARTED, blocks until stopped."""

    instances = []

    def __init__(self, sink):
        self.sink, self.args, self.stopped = sink, None, threading.Event()
        FakeService.instances.append(self)

    def serve(self, *args):
        self.args = args
        self.sink(LogEvent(Level.INFO, "10.0.0.5: requested '<img src=x onerror=alert(1)>' -> serving fw.bin (1 KB)"))
        self.sink(StateEvent(State.STARTED, "x"))
        self.stopped.wait(10)
        self.sink(StateEvent(State.STOPPED))

    def stop(self):
        self.stopped.set()


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "images"
    d.mkdir()
    (d / "fw.bin").write_bytes(b"x" * 100)
    (d / ".hidden").write_bytes(b"h")
    (d / "sub").mkdir()
    return d


@pytest.fixture
def panel(files):
    FakeService.instances = []
    server, controller = webui.serve_web(files, "127.0.0.1", 0, TOKEN, lambda: [Interface("eth0", "10.0.0.2")])
    controller._factories = {"tftp": FakeService, "http": FakeService, "https": FakeService}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], controller
    controller.shutdown()
    server.shutdown()
    server.server_close()


def call(port, method, path, body=None, token=TOKEN, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def wait_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_token_is_mandatory_and_must_be_long_enough(files):
    with pytest.raises(ValueError, match="at least"):
        webui.serve_web(files, "127.0.0.1", 0, "short", lambda: [])
    with pytest.raises(ValueError, match="not a directory"):
        webui.serve_web(files / "nope", "127.0.0.1", 0, TOKEN, lambda: [])
    assert len(webui.new_token()) >= webui.MIN_TOKEN


def test_api_requires_the_token(panel):
    port, _ = panel
    assert call(port, "GET", "/api/state", token=None)[0].status == 401
    assert call(port, "GET", "/api/state", token="wrong" * 6)[0].status == 401
    assert call(port, "POST", "/api/start", {"file": "fw.bin"}, token=None)[0].status == 401
    resp, data = call(port, "GET", "/api/state")
    assert resp.status == 200 and json.loads(data)["files"] == [{"name": "fw.bin", "size": 100}]


def test_failed_attempts_lock_the_client_out(files):
    t = webui._Throttle(limit=3, window=60)
    for _ in range(3):
        assert not t.locked("1.2.3.4")
        t.failed("1.2.3.4")
    assert t.locked("1.2.3.4") and not t.locked("5.6.7.8")
    clock = [0.0]
    t2 = webui._Throttle(limit=1, window=10, clock=lambda: clock[0])
    t2.failed("a")
    assert t2.locked("a")
    clock[0] = 11
    assert not t2.locked("a")  # the window expires


def test_page_is_public_but_carries_no_data_and_a_strict_csp(panel):
    port, _ = panel
    resp, body = call(port, "GET", "/", token=None)
    csp = resp.getheader("Content-Security-Policy")
    assert resp.status == 200 and "script-src 'nonce-" in csp and "default-src 'none'" in csp
    assert resp.getheader("X-Frame-Options") == "DENY" and resp.getheader("Cache-Control") == "no-store"
    assert b"fw.bin" not in body
    assert b"innerHTML" not in body  # device-controlled text must only ever be rendered with textContent
    nonce = csp.split("'nonce-")[1].split("'")[0].encode()
    assert b'nonce="' + nonce + b'"' in body


def test_start_and_stop_a_server_end_to_end(panel):
    port, _controller = panel
    resp, data = call(
        port,
        "POST",
        "/api/start",
        {
            "protocol": "http",
            "file": "fw.bin",
            "ip": "10.0.0.2",
            "port": "9000",
            "once": True,
            "idle": "30",
            "allow": "10.0.0.0/24",
        },
    )
    assert resp.status == 200
    job_id = json.loads(data)["id"]
    assert wait_until(lambda: json.loads(call(port, "GET", "/api/state")[1])["jobs"][0]["state"] == "running")
    args = FakeService.instances[0].args
    assert args[0].endswith("fw.bin") and args[1:3] == ("10.0.0.2", 9000)
    policy = args[3]
    assert policy.stop_after_first and policy.idle_timeout == 30.0 and str(policy.allowed[0]) == "10.0.0.0/24"
    job = json.loads(call(port, "GET", "/api/state")[1])["jobs"][0]
    assert (
        "requested '<img src=x onerror=alert(1)>'" in job["log"][0]
    )  # passed through verbatim as DATA (page escapes it)
    assert (
        call(port, "POST", "/api/start", {"protocol": "http", "file": "fw.bin", "port": 9000})[0].status == 409
    )  # port busy
    assert call(port, "POST", "/api/stop", {"id": job_id})[0].status == 200
    assert wait_until(lambda: json.loads(call(port, "GET", "/api/state")[1])["jobs"][0]["state"] == "stopped")
    assert call(port, "POST", "/api/stop", {"id": "nope"})[0].status == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"protocol": "ftp", "file": "fw.bin"},
        {"protocol": "tftp", "file": "../etc/passwd"},
        {"protocol": "tftp", "file": "/etc/passwd"},
        {"protocol": "tftp", "file": ".hidden"},
        {"protocol": "tftp", "file": "sub"},
        {"protocol": "tftp", "file": "fw.bin", "ip": "8.8.8.8"},
        {"protocol": "tftp", "file": "fw.bin", "port": "70000"},
        {"protocol": "tftp", "file": "fw.bin", "port": "abc"},
        {"protocol": "tftp", "file": "fw.bin", "allow": "not-a-cidr"},
        {"protocol": "tftp", "file": "fw.bin", "idle": "-5"},
    ],
)
def test_start_rejects_bad_input(panel, bad):
    port, _ = panel
    resp, data = call(port, "POST", "/api/start", bad)
    assert resp.status == 400 and "error" in json.loads(data)
    assert FakeService.instances == []  # nothing was started


def test_body_limits_and_malformed_json(panel):
    port, _ = panel
    assert call(port, "POST", "/api/start", raw="{not json")[0].status == 400
    assert call(port, "POST", "/api/start", raw="[1,2]")[0].status == 400
    assert call(port, "POST", "/api/start", raw=json.dumps({"x": "y" * 10_000}))[0].status == 413
    assert call(port, "GET", "/etc/passwd")[0].status == 404
    assert call(port, "POST", "/other", {})[0].status == 404


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinks_are_never_listed(panel, files, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    (files / "link.bin").symlink_to(outside)
    _, controller = panel
    assert [f["name"] for f in controller.files()] == ["fw.bin"]


def test_a_real_http_job_serves_the_file_through_the_panel(files):
    server, controller = webui.serve_web(files, "127.0.0.1", 0, TOKEN, lambda: [Interface("lo", "127.0.0.1")])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        job_id = controller.start({"protocol": "http", "file": "fw.bin", "ip": "127.0.0.1", "port": 18999})
        assert wait_until(lambda: controller.state()["jobs"][0]["state"] in ("running", "failed"))
        job = controller.state()["jobs"][0]
        if job["state"] == "failed":
            pytest.skip(f"port busy on this host: {job['detail']}")
        conn = http.client.HTTPConnection("127.0.0.1", 18999, timeout=10)
        conn.request("GET", "/anything")
        assert conn.getresponse().read() == b"x" * 100
        controller.stop(job_id)
        assert wait_until(lambda: controller.state()["jobs"][0]["state"] == "stopped")
    finally:
        controller.shutdown()
        server.shutdown()
        server.server_close()


def test_https_mode_serves_the_panel_over_tls(files):
    server, controller = webui.serve_web(files, "127.0.0.1", 0, TOKEN, lambda: [], https=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", server.server_address[1], timeout=10, context=ctx)
        conn.request("GET", "/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
        assert conn.getresponse().status == 200
    finally:
        controller.shutdown()
        server.shutdown()
        server.server_close()


def test_loopback_detection():
    assert webui.is_loopback("127.0.0.1") and webui.is_loopback("::1") and not webui.is_loopback("0.0.0.0")


@pytest.mark.hostile_dns
@pytest.mark.timeout(20)
def test_panel_starts_even_when_name_lookups_stall(files):
    server, controller = webui.serve_web(files, "127.0.0.1", 0, TOKEN, lambda: [])
    server.server_close()
    controller.shutdown()
