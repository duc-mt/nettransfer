"""Headless (offscreen) end-to-end checks: real tabs + real services + real sockets."""

import gc
import os
import socket
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import tftpy

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from app.core.audit import AuditLog
from app.core.hashing import HashService
from app.core.http_service import HttpService
from app.core.known_hosts import KnownHostsStore
from app.core.precheck import ConnectionTester
from app.core.requests import make_service_factory
from app.core.settings import SettingsStore
from app.core.tftp_service import TftpService
from app.platform.detect import detect_platform
from app.platform.network import Interface
from app.platform.privileges import probe_udp_bind
from app.ui.batch_tab import BatchTab
from app.ui.common import UiContext
from app.ui.http_tab import HttpTab
from app.ui.main_window import MainWindow
from app.ui.push_tab import PushTab
from app.ui.tftp_tab import TftpTab
from app.ui.widgets import FilePicker

_APP = None  # module-level reference: the QApplication must outlive every widget


@pytest.fixture(scope="session")
def qapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture
def track(qapp):
    """Register widgets so workers are always stopped and widgets destroyed in order
    (otherwise several widget trees are collected after the QApplication at exit and Qt segfaults)."""
    widgets = []
    yield widgets.append
    for w in widgets:
        if hasattr(w, "shutdown"):
            w.shutdown()
    for _ in range(5):
        QApplication.processEvents()
        time.sleep(0.01)
    for w in widgets:
        w.setParent(None)
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    widgets.clear()
    gc.collect()


@pytest.fixture
def ctx(tmp_path):
    c = UiContext(SettingsStore(tmp_path / "data" / "settings.json"), AuditLog(tmp_path / "data" / "logs"))
    c.notified = []
    c.notify = lambda title, message: c.notified.append((title, message))
    return c


@pytest.fixture
def warnings(monkeypatch):
    calls = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: calls.append(a[2] if len(a) > 2 else "")))
    return calls


def wait_until(cond, timeout_ms=10000):
    # NB: not QTest.qWait - it keeps the GIL while sleeping and starves the worker threads.
    deadline = time.monotonic() + timeout_ms / 1000
    while not cond() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert cond(), "condition not met in time"


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LO = lambda: [Interface("lo-test", "127.0.0.1", True)]  # noqa: E731


def make_tftp(ctx, port):
    return TftpTab(
        ctx=ctx,
        service_factory=lambda sink: TftpService(sink, select_timeout=0.5),
        hash_factory=HashService,
        list_interfaces=LO,
        probe=probe_udp_bind,
        platform_info=detect_platform(),
        port=port,
    )


def make_http(ctx):
    return HttpTab(ctx=ctx, service_factory=HttpService, hash_factory=HashService, list_interfaces=LO)


def make_push(ctx):
    kh = KnownHostsStore(None)
    return PushTab(
        ctx=ctx,
        services=lambda p: make_service_factory(p, kh),
        hash_factory=HashService,
        tester_factory=lambda sink: ConnectionTester(sink, kh),
    )


def make_batch(ctx):
    kh = KnownHostsStore(None)
    return BatchTab(ctx=ctx, services=lambda p: make_service_factory(p, kh), hash_factory=HashService)


# ---------------------------------------------------------------- TFTP tab
def test_tftp_tab_end_to_end(qapp, track, ctx, tmp_path):
    payload = os.urandom(150_000)
    src = tmp_path / "fw.bin"
    src.write_bytes(payload)
    port = free_port(socket.SOCK_DGRAM)
    tab = make_tftp(ctx, port)
    track(tab)
    tab._file.set_path(str(src))
    assert "copy tftp://127.0.0.1/fw.bin flash:fw.bin" in tab._hints._pull.text()  # hint follows file + IP

    tab._action.click()
    wait_until(lambda: "Listening on 127.0.0.1" in tab._status.text())
    assert tab._action.text() == "Stop server" and not tab._file.isEnabled() and not tab._safety.isEnabled()

    out = tmp_path / "out.bin"
    t = threading.Thread(target=lambda: tftpy.TftpClient("127.0.0.1", port).download("x.img", str(out)))
    t.start()
    wait_until(lambda: tab._panel._bar.value() == 1000)
    t.join(10)
    assert out.read_bytes() == payload
    wait_until(lambda: "sent" in tab._panel.log_text())
    wait_until(lambda: ctx.notified)
    assert ctx.notified[0][0] == "TFTP: transfer complete"
    assert "requested 'x.img'" in ctx.audit.path.read_text()  # audit trail written

    tab._action.click()
    wait_until(lambda: tab._action.text() == "Start server" and tab._action.isEnabled())
    assert not tab._runner.running and tab._file.isEnabled() and tab._status.text() == "Stopped"


def test_tftp_serve_once_returns_to_idle_by_itself(qapp, track, ctx, tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(20_000))
    port = free_port(socket.SOCK_DGRAM)
    tab = make_tftp(ctx, port)
    track(tab)
    tab._file.set_path(str(src))
    tab._safety._once.setChecked(True)
    tab._action.click()
    wait_until(lambda: "Listening" in tab._status.text())
    threading.Thread(target=lambda: tftpy.TftpClient("127.0.0.1", port).download("a", str(tmp_path / "o"))).start()
    wait_until(lambda: tab._action.text() == "Start server" and tab._action.isEnabled(), 15000)
    assert "Serve-once" in tab._panel.log_text()


def test_tftp_invalid_allowlist_blocks_start(qapp, track, ctx, tmp_path, warnings):
    src = tmp_path / "f.bin"
    src.write_bytes(b"abc")
    tab = make_tftp(ctx, free_port(socket.SOCK_DGRAM))
    track(tab)
    tab._file.set_path(str(src))
    tab._safety._allow.setText("10.0.0.0/24, banana")
    tab._action.click()
    assert warnings and "banana" in warnings[0] and not tab._runner.running


def test_tftp_hash_button_logs_digests(qapp, track, ctx, tmp_path):
    src = tmp_path / "h.bin"
    src.write_bytes(b"abc")
    tab = make_tftp(ctx, free_port(socket.SOCK_DGRAM))
    track(tab)
    tab._file.set_path(str(src))
    tab._file._hash.click()
    wait_until(lambda: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" in tab._panel.log_text())


def test_tftp_bind_failure_is_reported_and_recoverable(qapp, track, ctx, tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"abc")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        tab = make_tftp(ctx, blocker.getsockname()[1])
        track(tab)
        assert not tab._banner.isHidden() and "already in use" in tab._banner.text()
        tab._file.set_path(str(src))
        tab._action.click()
        wait_until(lambda: tab._status.text().startswith("Failed:"))
        wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Start server")


# ---------------------------------------------------------------- HTTP tab
def test_http_tab_serves_and_hint_follows_options(qapp, track, ctx, tmp_path):
    import urllib.request

    payload = os.urandom(120_000)
    src = tmp_path / "image.bin"
    src.write_bytes(payload)
    port = free_port(socket.SOCK_STREAM)
    tab = make_http(ctx)
    track(tab)
    tab._file.set_path(str(src))
    tab._port_spin.setValue(port)
    tab._hints._vendor.setCurrentIndex(2)  # Juniper Junos
    assert tab._hints._pull.text() == f"file copy http://127.0.0.1:{port}/image.bin /var/tmp/image.bin"
    tab._https.setChecked(True)
    assert tab._hints._pull.text().startswith("file copy https://")
    tab._https.setChecked(False)
    tab._action.click()
    wait_until(lambda: f"Listening on http://127.0.0.1:{port}" in tab._status.text())
    got = []
    t = threading.Thread(
        target=lambda: got.append(urllib.request.urlopen(f"http://127.0.0.1:{port}/a", timeout=10).read())
    )
    t.start()
    wait_until(lambda: tab._panel._bar.value() == 1000)
    t.join(10)
    assert got == [payload]
    tab._action.click()
    wait_until(lambda: tab._action.text() == "Start server" and tab._action.isEnabled())


# ---------------------------------------------------------------- Push tab
def test_push_tab_connection_test_button(qapp, track, ctx, ssh_stub):
    port, _ = ssh_stub  # the stub serves a single connection, so this test does not push
    tab = make_push(ctx)
    track(tab)
    tab._host.setText("127.0.0.1")
    tab._options._port.setValue(port)
    tab._test.click()
    wait_until(lambda: "not seen before" in tab._panel.log_text())
    wait_until(lambda: tab._test.isEnabled())


def test_push_tab_scp_end_to_end(qapp, track, ctx, tmp_path, ssh_stub):
    port, state = ssh_stub
    payload = os.urandom(120_000)
    src = tmp_path / "cfg.bin"
    src.write_bytes(payload)
    tab = make_push(ctx)
    track(tab)
    tab._file.set_path(str(src))
    tab._host.setText("127.0.0.1")
    tab._options._user.setText("admin")
    tab._options._password.setText("secret")
    tab._options._port.setValue(port)

    tab._action.click()
    wait_until(lambda: tab._status.text() == "Done")
    wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Push file")
    assert tab._panel._bar.value() == 1000 and state["received"] == payload
    assert "secret" not in tab._panel.log_text() and "secret" not in ctx.audit.path.read_text()
    assert any(t == "PUSH: transfer complete" for t, _ in ctx.notified)


def test_push_tab_bad_password_and_validation(qapp, track, ctx, tmp_path, ssh_stub, warnings):
    port, _ = ssh_stub
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    tab = make_push(ctx)
    track(tab)
    tab._action.click()  # nothing filled in
    assert warnings and "existing file" in warnings[-1]
    tab._file.set_path(str(src))
    tab._host.setText("127.0.0.1")
    tab._options._user.setText("admin")
    tab._action.click()
    assert "password or choose an SSH key" in warnings[-1]
    tab._options._password.setText("wrong")
    tab._options._port.setValue(port)
    tab._action.click()
    wait_until(lambda: tab._status.text().startswith("Failed: Authentication failed"))
    wait_until(lambda: tab._action.isEnabled())
    assert any("push failed" in t for t, _ in ctx.notified)


def test_push_options_follow_protocol(qapp, track, ctx):
    tab = make_push(ctx)
    track(tab)
    opts = tab._options
    assert opts.port() == 22 and not opts._key_row.isHidden()
    opts._protocol.setCurrentIndex(opts._protocol.findData("ftp"))
    assert opts.port() == 21 and opts._key_row.isHidden() and opts._passphrase.isHidden()
    opts._port.setValue(2121)  # a custom port must survive a protocol switch
    opts._protocol.setCurrentIndex(opts._protocol.findData("sftp"))
    assert opts.port() == 2121 and not opts._key_row.isHidden()
    opts._preset.activated.emit(opts._preset.findText("Cisco IOS / IOS-XE"))
    assert opts._remote.text() == "flash:"


# ---------------------------------------------------------------- Batch tab
def test_batch_tab_runs_retries_and_exports(qapp, track, ctx, tmp_path, ssh_stub, monkeypatch, warnings):
    port, state = ssh_stub
    dead = free_port(socket.SOCK_STREAM)
    payload = os.urandom(80_000)
    src = tmp_path / "image.bin"
    src.write_bytes(payload)
    targets = tmp_path / "t.csv"
    targets.write_text(f"host,port\n127.0.0.1,{port}\n127.0.0.1,{dead}\n")
    tab = make_batch(ctx)
    track(tab)
    tab._file.set_path(str(src))
    tab._csv.set_path(str(targets))
    tab._options._user.setText("admin")
    tab._options._password.setText("secret")
    assert not tab._action.isEnabled()  # nothing to run until targets are loaded
    tab._load.click()
    assert tab._table.rowCount() == 2 and tab._table.item(0, 1).text() == "ready"

    tab._action.click()
    wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Start batch")
    assert tab._table.item(0, 1).text() == "done" and tab._table.item(0, 2).text() == "100%"
    assert tab._table.item(1, 1).text() == "failed" and "refused" in tab._table.item(1, 3).text().lower()
    assert "1 ok, 1 failed" in tab._summary.text()
    assert state["received"] == payload
    assert tab._retry.isEnabled() and tab._export.isEnabled()
    assert any(t == "BATCH: batch finished" for t, _ in ctx.notified)

    out = tmp_path / "res.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    tab._export.click()
    assert "failed" in out.read_text() and "done" in out.read_text()


def test_batch_tab_rejects_bad_csv(qapp, track, ctx, tmp_path, warnings):
    bad = tmp_path / "bad.csv"
    bad.write_text("host,port\n1.1.1.1,x\n")
    tab = make_batch(ctx)
    track(tab)
    tab._csv.set_path(str(bad))
    tab._load.click()
    assert warnings and "invalid port" in warnings[0] and tab._table.rowCount() == 0


# ---------------------------------------------------------------- widgets / window
def test_file_picker_accepts_dropped_file(qapp, track, tmp_path):
    f = tmp_path / "dropped.bin"
    f.write_bytes(b"x")
    picker = FilePicker()
    track(picker)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path)), QUrl.fromLocalFile(str(f))])  # a folder is ignored
    picker.dropEvent(QDropEvent(QPointF(1, 1), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier))
    assert picker.path() == str(f)


def test_settings_persist_across_sessions_but_never_passwords(qapp, track, ctx, tmp_path):
    f = tmp_path / "keep.bin"
    f.write_bytes(b"x")
    push, tftp = make_push(ctx), make_tftp(ctx, free_port(socket.SOCK_DGRAM))
    win = MainWindow([("TFTP", tftp), ("Push", push)], detect_platform(), ctx)
    track(win)
    push._file.set_path(str(f))
    push._host.setText("10.1.1.1")
    push._options._user.setText("ops")
    push._options._password.setText("hunter2")
    push._options._passphrase.setText("hunter3")
    tftp._safety._allow.setText("10.0.0.0/8")
    win.close()
    raw = ctx.settings.path.read_text()
    assert "hunter2" not in raw and "hunter3" not in raw and "10.1.1.1" in raw

    ctx2 = UiContext(SettingsStore(ctx.settings.path), AuditLog(None))
    push2, tftp2 = make_push(ctx2), make_tftp(ctx2, free_port(socket.SOCK_DGRAM))
    track(push2)
    track(tftp2)
    assert push2._host.text() == "10.1.1.1" and push2._file.path() == str(f)
    assert push2._options._user.text() == "ops" and push2._options._password.text() == ""
    assert tftp2._safety._allow.text() == "10.0.0.0/8"


def test_main_window_close_stops_server(qapp, track, ctx, tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"abc")
    port = free_port(socket.SOCK_DGRAM)
    tftp = make_tftp(ctx, port)
    win = MainWindow([("TFTP", tftp), ("Push", make_push(ctx))], detect_platform(), ctx)
    track(win)
    win.show()
    tftp._file.set_path(str(src))
    tftp._action.click()
    wait_until(lambda: "Listening" in tftp._status.text())
    started = time.monotonic()
    win.close()
    assert time.monotonic() - started < 3, "closing must not hang on the worker"
    wait_until(lambda: not tftp._runner.running)
    assert probe_udp_bind("127.0.0.1", port).ok  # port released
