"""Headless (offscreen) end-to-end checks: real tabs + real services + real sockets."""

import gc
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import tftpy

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from app.core.audit import AuditLog
from app.core.devicecheck import DeviceCheckService
from app.core.events import State, StateEvent
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
        checker_factory=lambda sink: DeviceCheckService(sink, kh),
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


# ---------------------------------------------------------------- device checks, inventories, retries (GUI)
def lab_digest(lab, wrong=False):
    """Cisco-style 'verify /md5' answer computed from what the fake device received."""

    def handler(command):
        if command.startswith("verify /md5"):
            name = command.rsplit(":", 1)[-1]
            digest = "e" * 32 if wrong else hashlib.md5(lab.files[name]).hexdigest()
            return f"verify /md5 ({command.split()[-1]}) = {digest}\n", 0
        if command.startswith("dir "):
            return "9000000000 bytes total (8000000000 bytes free)\n", 0
        return "", 0

    return handler


def fill_push(tab, lab, src, remote="flash:"):
    tab._file.set_path(str(src))
    tab._host.setText("127.0.0.1")
    opts = tab._options
    opts._user.setText("admin")
    opts._password.setText("secret")
    opts._port.setValue(lab.port)
    opts._remote.setText(remote)
    return opts


def choose_vendor(opts, vendor_id):
    opts._vendor.setCurrentIndex(opts._vendor.findData(vendor_id))


def test_device_checks_panel_values_visibility_and_validation(qapp, track):
    from app.ui.push_options import PushOptionsPanel

    opts = PushOptionsPanel()
    track(opts)
    d = opts.defaults()
    assert (d.vendor, d.preflight, d.verify, d.resume) == ("", False, None, False)  # nothing on by default
    assert opts.problem() == ""

    choose_vendor(opts, "cisco-ios")
    opts._preflight.setChecked(True)
    opts._verify.setChecked(True)
    d = opts.defaults()
    assert d.vendor == "cisco-ios" and d.preflight and d.verify is not None and d.verify.command == ""
    assert opts.problem() == ""

    choose_vendor(opts, "")  # verify without any way to know the command
    assert "vendor" in opts.problem()
    assert opts.problem(vendors_from_targets=True) == ""  # a batch whose devices all carry a vendor is fine
    choose_vendor(opts, "fortinet")  # a vendor with no built-in verify command
    assert "no built-in verify command" in opts.problem()
    opts._verify_cmd.setText("md5sum {path}")
    assert opts.problem() == "" and opts.defaults().verify.command == "md5sum {path}"
    opts._verify_cmd.setText("md5sum")
    assert "{path}" in opts.problem()
    opts._verify_cmd.setText("md5sum {nope}")
    assert "{nope}" in opts.problem()  # unknown placeholder: reported, not a crash later

    # SCP cannot resume; FTP has no SSH to run commands over
    opts._resume.setChecked(True)
    assert opts._protocol.currentData() == "scp" and opts._resume.isHidden() and not opts.defaults().resume
    opts._protocol.setCurrentIndex(opts._protocol.findData("sftp"))
    assert not opts._resume.isHidden() and opts.defaults().resume
    opts._protocol.setCurrentIndex(opts._protocol.findData("ftp"))
    d = opts.defaults()
    assert opts._checks.isHidden() and (d.vendor, d.preflight, d.verify) == ("", False, None) and d.resume
    assert opts.problem() == ""


def test_device_checks_settings_persist_but_never_secrets(qapp, track, ctx, tmp_path):
    from app.ui.push_options import PushOptionsPanel

    a = PushOptionsPanel()
    track(a)
    choose_vendor(a, "junos")
    a._preflight.setChecked(True)
    a._verify.setChecked(True)
    a._verify_cmd.setText("file checksum sha-256 {path}")
    a._resume.setChecked(True)
    a._password.setText("hunter2")
    a.save(ctx.settings, "push")
    ctx.settings.save()
    b = PushOptionsPanel()
    track(b)
    b.load(ctx.settings, "push")
    assert b._vendor.currentData() == "junos" and b._preflight.isChecked() and b._verify.isChecked()
    assert b._verify_cmd.text() == "file checksum sha-256 {path}" and b._resume.isChecked()
    assert not b._checks.isHidden() and b._checks.isChecked()  # opened, because it holds a saved choice
    assert b._password.text() == "" and "hunter2" not in (tmp_path / "data" / "settings.json").read_text()
    ctx.settings.set("push.vendor", "no-such-vendor")  # a stale/edited settings file must not break the panel
    c = PushOptionsPanel()
    track(c)
    c.load(ctx.settings, "push")
    assert c._vendor.currentData() == ""


def test_push_tab_verify_ok_and_mismatch(qapp, track, ctx, tmp_path, ssh_lab):
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(60_000))
    tab = make_push(ctx)
    track(tab)
    opts = fill_push(tab, ssh_lab, src)
    choose_vendor(opts, "cisco-ios")
    opts._preflight.setChecked(True)
    opts._verify.setChecked(True)
    ssh_lab.commands = lab_digest(ssh_lab)
    tab._action.click()
    wait_until(lambda: tab._status.text() == "Done - VERIFIED (hash match)")
    wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Push file")
    assert "VERIFIED (hash match)" in tab._panel.log_text() and "Pre-flight" in tab._panel.log_text()
    assert tab._status.text() == "Done - VERIFIED (hash match)"
    assert "verify /md5 flash:fw.bin" in ssh_lab.executed and "dir flash:" in ssh_lab.executed
    assert "VERIFIED (hash match): fw.bin" in ctx.audit.path.read_text()  # lands in the audit trail / history report

    ssh_lab.commands = lab_digest(ssh_lab, wrong=True)
    tab._action.click()
    wait_until(lambda: tab._status.text().startswith("FAILED (checksum mismatch)"))
    wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Push file")
    assert any("push failed" in t for t, _ in ctx.notified)


def test_push_tab_preflight_stops_when_there_is_no_room(qapp, track, ctx, tmp_path, ssh_lab):
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(50_000))
    tab = make_push(ctx)
    track(tab)
    opts = fill_push(tab, ssh_lab, src)
    choose_vendor(opts, "cisco-ios")
    opts._preflight.setChecked(True)
    ssh_lab.commands = lambda command: ("1000000 bytes total (100 bytes free)\n", 0)
    tab._action.click()
    wait_until(lambda: tab._status.text().startswith("Failed: Not enough space"))
    wait_until(lambda: tab._action.isEnabled())
    assert ssh_lab.files == {}  # nothing was sent


def test_push_tab_refuses_verify_without_a_way_to_verify(qapp, track, ctx, tmp_path, ssh_lab, warnings):
    src = tmp_path / "fw.bin"
    src.write_bytes(b"x" * 100)
    tab = make_push(ctx)
    track(tab)
    opts = fill_push(tab, ssh_lab, src)
    opts._verify.setChecked(True)  # no vendor, no custom command
    tab._action.click()
    assert warnings and "Verify needs a vendor" in warnings[-1] and ssh_lab.files == {}


def test_push_tab_sftp_resume_with_verify(qapp, track, ctx, tmp_path, ssh_lab):
    src = tmp_path / "fw.bin"
    payload = os.urandom(150_000)
    src.write_bytes(payload)
    Path(ssh_lab.root, "fw.bin").write_bytes(payload[:40_000])  # a partial upload from earlier
    tab = make_push(ctx)
    track(tab)
    opts = fill_push(tab, ssh_lab, src, remote="/")
    opts._protocol.setCurrentIndex(opts._protocol.findData("sftp"))
    opts._port.setValue(ssh_lab.port)
    opts._resume.setChecked(True)
    tab._action.click()
    wait_until(lambda: tab._status.text() == "Done")
    assert "Resuming at" in tab._panel.log_text()
    assert Path(ssh_lab.root, "fw.bin").read_bytes() == payload


def write_inventory(tmp_path, ports, vendors=True):
    entries = [
        {"host": "127.0.0.1", "port": ports[0], "username": "admin", "site": "A", "name": "sw-a"},
        {"host": "127.0.0.1", "port": ports[1], "username": "admin", "site": "B", "name": "sw-b"},
    ]
    if vendors:
        for e in entries:
            e["device_type"] = "cisco_ios"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(entries))
    return path


def test_batch_tab_loads_inventory_with_filter_and_tooltips(qapp, track, ctx, tmp_path, warnings):
    inv = write_inventory(tmp_path, [1111, 2222])
    tab = make_batch(ctx)
    track(tab)
    tab._csv.set_path(str(inv))
    tab._load.click()
    assert tab._table.rowCount() == 2 and "site:A" in tab._table.item(0, 0).toolTip()
    assert "vendor: cisco-ios" in tab._table.item(0, 0).toolTip()

    tab._filter.setText("site:B")
    tab._load.click()
    assert tab._table.rowCount() == 1 and "2 devices" not in tab._summary.text() and "1 devices" in tab._summary.text()
    assert "Loaded 1 of 2 devices (filter: site:B)" in tab._panel.log_text()
    assert tab._targets[0].index == 0 and tab._targets[0].port == 2222  # re-indexed, still the right device

    tab._filter.setText("site:Z")
    tab._load.click()
    assert warnings and "matches none of the 2 devices" in warnings[-1]
    assert tab._table.rowCount() == 1  # a bad filter leaves the previous selection alone


def test_batch_tab_loads_yaml_inventory(qapp, track, ctx, tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "hosts.yml"
    path.write_text(
        "all:\n  children:\n    dctt:\n      hosts:\n        sw1: {ansible_host: 10.1.0.1, ansible_user: ops}\n"
    )
    tab = make_batch(ctx)
    track(tab)
    tab._csv.set_path(str(path))
    tab._load.click()
    assert tab._table.item(0, 0).text() == "10.1.0.1" and "group:dctt" in tab._table.item(0, 0).toolTip()


def test_batch_tab_verify_uses_per_device_vendors_from_the_inventory(qapp, track, ctx, tmp_path, ssh_lab, warnings):
    from tests.conftest import SshLab

    other = SshLab(tmp_path / "other")
    try:
        src = tmp_path / "fw.bin"
        src.write_bytes(os.urandom(40_000))
        for lab in (ssh_lab, other):
            lab.commands = lab_digest(lab)
        tab = make_batch(ctx)
        track(tab)
        tab._file.set_path(str(src))
        tab._csv.set_path(str(write_inventory(tmp_path, [ssh_lab.port, other.port])))
        tab._options._password.setText("secret")
        tab._options._remote.setText("flash:")
        tab._options._verify.setChecked(True)  # default vendor stays "None": each device brings cisco-ios itself
        tab._load.click()
        tab._action.click()
        wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Start batch")
        assert [tab._table.item(i, 1).text() for i in range(2)] == ["done", "done"]
        assert "verify /md5 flash:fw.bin" in ssh_lab.executed and "verify /md5 flash:fw.bin" in other.executed
        assert "VERIFIED (hash match)" in tab._panel.log_text()
        assert [tab._table.item(i, 3).text() for i in range(2)] == ["VERIFIED (hash match)"] * 2
    finally:
        other.close()


def test_batch_tab_verify_without_any_vendor_is_refused(qapp, track, ctx, tmp_path, ssh_lab, warnings):
    src = tmp_path / "fw.bin"
    src.write_bytes(b"x" * 100)
    tab = make_batch(ctx)
    track(tab)
    tab._file.set_path(str(src))
    tab._csv.set_path(str(write_inventory(tmp_path, [ssh_lab.port, ssh_lab.port], vendors=False)))
    tab._options._password.setText("secret")
    tab._options._verify.setChecked(True)
    tab._load.click()
    tab._action.click()
    assert warnings and "Verify needs a vendor" in warnings[-1] and ssh_lab.files == {}


def test_batch_tab_retries_transient_failures_and_shows_the_wait(qapp, track, ctx, tmp_path):
    attempts = []

    class Flaky:
        def __init__(self, sink):
            self.sink = sink

        def push(self, request):
            attempts.append(request)
            if len(attempts) < 3:
                self.sink(StateEvent(State.FAILED, "connection reset", True))
            else:
                self.sink(StateEvent(State.STARTED, ""))
                self.sink(StateEvent(State.FINISHED, ""))

        def cancel(self):
            pass

    src = tmp_path / "fw.bin"
    src.write_bytes(b"x" * 10)
    targets = tmp_path / "t.csv"
    targets.write_text("host\n10.9.9.9\n")
    tab = make_batch(ctx)
    track(tab)
    tab._services = lambda protocol: Flaky
    tab._file.set_path(str(src))
    tab._csv.set_path(str(targets))
    tab._options._user.setText("admin")
    tab._options._password.setText("secret")
    tab._retries.setValue(2)
    tab._load.click()
    tab._action.click()
    wait_until(lambda: "retry 1/2" in tab._panel.log_text() or "retrying in" in tab._panel.log_text(), 15000)
    wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Start batch", 20000)
    assert len(attempts) == 3 and tab._table.item(0, 1).text() == "done"
    assert "1 ok, 0 failed" in tab._summary.text()


def test_batch_settings_remember_filter_and_retries(qapp, track, ctx):
    a = make_batch(ctx)
    track(a)
    a._filter.setText("site:DCTT")
    a._retries.setValue(3)
    a.save_settings()
    b = make_batch(ctx)
    track(b)
    assert b._filter.text() == "site:DCTT" and b._retries.value() == 3


def test_hint_panel_lists_the_new_vendors_and_shows_notes(qapp):
    from app.core.device_hints import hints_for
    from app.ui.widgets import HintPanel

    panel = HintPanel(hints_for("http"))
    names = [panel._vendor.itemText(i) for i in range(panel._vendor.count())]
    assert {"Arista EOS", "MikroTik RouterOS", "Aruba AOS-CX (SFTP pull)", "Palo Alto PAN-OS (SCP pull)"} <= set(names)
    panel.update_context(ip="10.0.0.5", port=8080, scheme="http", file="fw.bin")
    panel._vendor.setCurrentIndex(names.index("MikroTik RouterOS"))
    assert panel._pull.text() == '/tool fetch url="http://10.0.0.5:8080/fw.bin" mode=http'
    assert not panel._note.isHidden() and "check-certificate=no" in panel._note.text()
    panel._vendor.setCurrentIndex(names.index("Arista EOS"))
    assert panel._note.isHidden() and panel._verify.text() == "verify /sha256 flash:fw.bin"
    panel.deleteLater()


# ---------------------------------------------------------------- boot-image / reload hooks and their confirmation (GUI)
@pytest.fixture
def confirm(monkeypatch):
    """Replace the modal yes/no dialog; ``confirm.answer`` decides, ``confirm.texts`` records what was asked."""
    from PySide6.QtWidgets import QMessageBox

    class Dialog:
        answer = QMessageBox.StandardButton.Yes
        texts = []

    def question(_parent, _title, text, *_a, **_k):
        Dialog.texts.append(text)
        return Dialog.answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    Dialog.texts = []
    return Dialog


def test_post_action_controls_map_to_install_options_and_start_as_a_dry_run(qapp, track):
    from app.ui.push_options import PushOptionsPanel

    opts = PushOptionsPanel()
    track(opts)
    opts._checks.setChecked(True)  # a collapsed (unchecked) group box disables its children
    assert opts._checks.title() == "Device checks (SSH)"
    assert opts.defaults().install is None and opts.confirmation_text("x") == ""  # nothing intrusive by default
    opts._boot.setChecked(True)
    assert opts._checks.title().endswith("in use")  # visible even when the group is collapsed
    choose_vendor(opts, "cisco-ios")
    install = opts.defaults().install
    assert install.set_boot and not install.execute and install.reload_in is None and not install.reload  # dry run
    assert opts.confirmation_text("10.0.0.1") == ""  # a dry run changes nothing, so it asks nothing

    opts._dry_run.setChecked(False)
    opts._verify.setChecked(True)
    opts._reload_mode.setCurrentIndex(opts._reload_mode.findData("scheduled"))
    assert opts._reload_minutes.isEnabled()
    opts._reload_minutes.setValue(15)
    install = opts.defaults().install
    assert install.execute and install.set_boot and install.reload_in == 15 and not install.reload
    assert not install.confirmed and opts.defaults(confirmed=True).install.confirmed
    text = opts.confirmation_text("10.0.0.1")
    assert "10.0.0.1" in text and "schedule a reload in 15 minute(s)" in text and "set the boot image" in text

    opts._boot.setChecked(False)
    opts._reload_mode.setCurrentIndex(opts._reload_mode.findData("now"))
    assert not opts._reload_minutes.isEnabled()
    install = opts.defaults().install
    assert install.reload and install.reload_in is None and not install.set_boot
    assert "RELOAD the device immediately" in opts.confirmation_text("x")

    opts._protocol.setCurrentIndex(opts._protocol.findData("ftp"))
    assert opts.defaults().install is None and opts.problem() == ""  # FTP has no SSH: the hooks do not apply


def test_post_action_validation_messages(qapp, track):
    from app.ui.push_options import PushOptionsPanel

    opts = PushOptionsPanel()
    track(opts)
    opts._boot.setChecked(True)
    assert "Choose a vendor" in opts.problem()  # even a dry run needs a vendor to know the commands
    choose_vendor(opts, "cisco-ios")
    assert opts.problem() == ""  # dry run: nothing is changed, so verification is not required
    choose_vendor(opts, "")
    opts._dry_run.setChecked(False)
    assert "needs 'Verify afterwards'" in opts.problem()  # a corrupt file must never become the boot image
    opts._verify.setChecked(True)
    opts._verify_cmd.setText("md5sum {path}")
    assert "Choose a vendor" in opts.problem() and opts.problem(vendors_from_targets=True) == ""
    choose_vendor(opts, "fortinet")
    assert "no boot-image commands" in opts.problem()
    choose_vendor(opts, "cisco-nxos")
    opts._boot.setChecked(False)
    opts._reload_mode.setCurrentIndex(opts._reload_mode.findData("scheduled"))
    assert "no scheduled reload" in opts.problem()
    opts._reload_mode.setCurrentIndex(opts._reload_mode.findData("now"))
    assert opts.problem() == ""
    choose_vendor(opts, "linux")
    assert "no reload command" in opts.problem()


def test_post_action_choices_are_never_remembered(qapp, track, ctx):
    from app.ui.push_options import PushOptionsPanel

    a = PushOptionsPanel()
    track(a)
    a._boot.setChecked(True)
    a._dry_run.setChecked(False)
    a._reload_mode.setCurrentIndex(a._reload_mode.findData("now"))
    a.save(ctx.settings, "push")
    ctx.settings.save()
    b = PushOptionsPanel()
    track(b)
    b.load(ctx.settings, "push")
    assert not b._boot.isChecked() and b._dry_run.isChecked() and b._reload_mode.currentData() == "none"


def lab_with_digest(lab):
    lab.commands = lab_digest(lab)


def run_push_with_hooks(tab, lab, src, **hooks):
    opts = fill_push(tab, lab, src)
    choose_vendor(opts, "cisco-ios")
    opts._verify.setChecked(True)
    opts._boot.setChecked(hooks.get("boot", False))
    opts._reload_mode.setCurrentIndex(opts._reload_mode.findData(hooks.get("reload", "none")))
    opts._reload_minutes.setValue(hooks.get("minutes", 5))
    opts._dry_run.setChecked(hooks.get("dry_run", True))
    tab._action.click()
    return opts


def test_push_tab_dry_run_shows_the_plan_and_asks_nothing(qapp, track, ctx, tmp_path, ssh_lab, confirm):
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(30_000))
    lab_with_digest(ssh_lab)
    tab = make_push(ctx)
    track(tab)
    run_push_with_hooks(tab, ssh_lab, src, boot=True, reload="scheduled", minutes=10)
    wait_until(lambda: tab._status.text().startswith("Done"))
    log = tab._panel.log_text()
    assert "boot system flash:fw.bin" in log and "reload in 10" in log and "dry run" in log
    assert confirm.texts == [] and ssh_lab.shell_log == []


def test_push_tab_declined_confirmation_changes_and_sends_nothing(qapp, track, ctx, tmp_path, ssh_lab, confirm):
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(30_000))
    lab_with_digest(ssh_lab)
    tab = make_push(ctx)
    track(tab)
    from PySide6.QtWidgets import QMessageBox

    confirm.answer = QMessageBox.StandardButton.No
    run_push_with_hooks(tab, ssh_lab, src, boot=True, dry_run=False)
    assert len(confirm.texts) == 1 and "127.0.0.1" in confirm.texts[0]
    assert ssh_lab.files == {} and ssh_lab.shell_log == [] and tab._action.text() == "Push file"  # nothing started


def test_push_tab_confirmed_boot_and_scheduled_reload_are_applied(qapp, track, ctx, tmp_path, ssh_lab, confirm):
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(30_000))
    lab_with_digest(ssh_lab)
    tab = make_push(ctx)
    track(tab)
    run_push_with_hooks(tab, ssh_lab, src, boot=True, reload="scheduled", minutes=7, dry_run=False)
    wait_until(lambda: tab._status.text().startswith("Done"), 15000)
    assert len(confirm.texts) == 1
    assert ssh_lab.shell_log == [
        "configure terminal", "no boot system", "boot system flash:fw.bin", "end", "write memory", "reload in 7",
    ]  # fmt: skip
    assert "Install plan applied" in tab._panel.log_text()


def test_batch_tab_asks_once_for_all_devices_and_applies_to_each(qapp, track, ctx, tmp_path, ssh_lab, confirm):
    from tests.conftest import SshLab

    other = SshLab(tmp_path / "other")
    try:
        src = tmp_path / "fw.bin"
        src.write_bytes(os.urandom(30_000))
        for lab in (ssh_lab, other):
            lab.commands = lab_digest(lab)
        tab = make_batch(ctx)
        track(tab)
        tab._file.set_path(str(src))
        tab._csv.set_path(str(write_inventory(tmp_path, [ssh_lab.port, other.port])))
        opts = tab._options
        opts._password.setText("secret")
        opts._remote.setText("flash:")
        opts._verify.setChecked(True)
        opts._boot.setChecked(True)
        opts._dry_run.setChecked(False)
        tab._load.click()
        tab._action.click()
        wait_until(lambda: tab._action.isEnabled() and tab._action.text() == "Start batch", 20000)
        assert len(confirm.texts) == 1 and "2 device(s)" in confirm.texts[0]
        assert "boot system flash:fw.bin" in ssh_lab.shell_log and "boot system flash:fw.bin" in other.shell_log
    finally:
        other.close()


def test_every_vendor_profile_is_offered_and_verify_validation_matches_each(qapp, track):
    from app.core.vendors import VENDORS
    from app.ui.push_options import PushOptionsPanel

    opts = PushOptionsPanel()
    track(opts)
    offered = {opts._vendor.itemData(i) for i in range(opts._vendor.count())}
    assert set(VENDORS) <= offered  # a new platform profile must appear in the dropdown without extra wiring
    opts._verify.setChecked(True)
    # Huawei verifies through an interactive 'check system-software' (no plain verify command): that is still built in
    choose_vendor(opts, "huawei")
    assert opts.problem() == ""
    for with_command in ("cisco-ios", "arista-eos", "vyos", "pfsense", "opnsense", "checkpoint-gaia", "aruba-aos-cx"):
        choose_vendor(opts, with_command)
        assert opts.problem() == "", with_command
    # platforms with no on-box file hash say so instead of failing mid-push
    for without in ("fortinet", "paloalto-panos", "ruijie-rgos"):
        choose_vendor(opts, without)
        assert "no built-in verify command" in opts.problem(), without
    opts._verify_cmd.setText("md5sum {path}")
    assert opts.problem() == ""  # a custom command always works


# ---------------------------------------------------------------- "Check device": verify without pushing (GUI)
def checking(tab, lab, src=None, vendor="fortinet", expect=""):
    """Fill the Push tab for a device check; ``src`` None = no file selected."""
    opts = fill_push(tab, lab, src or "")
    if src is None:
        tab._file.set_path("")
    choose_vendor(opts, vendor)
    tab._expect.setText(expect)
    return opts


def finish_check(tab, status_prefix):
    wait_until(lambda: tab._status.text().startswith(status_prefix))
    wait_until(lambda: tab._check.isEnabled() and tab._check.text() == "Check device")


def test_check_device_confirms_the_version_without_sending_anything(qapp, track, ctx, ssh_lab):
    ssh_lab.commands = lambda c: ("Version: FortiGate-60E v7.2.4,build1396,230214 (GA.M)\n", 0)
    tab = make_push(ctx)
    track(tab)
    checking(tab, ssh_lab, expect="7.2.4")
    tab._check.click()
    finish_check(tab, "Check passed")
    assert "VERIFIED (version match)" in tab._panel.log_text()
    assert ssh_lab.executed == ["get system status"] and ssh_lab.files == {} and ssh_lab.shell_log == []
    assert tab._action.isEnabled() and tab._action.text() == "Push file"  # the push button is back to normal


def test_check_device_reports_a_wrong_version_as_a_failure(qapp, track, ctx, ssh_lab):
    ssh_lab.commands = lambda c: ("Version: FortiGate-60E v7.0.12,build0523\n", 0)
    tab = make_push(ctx)
    track(tab)
    checking(tab, ssh_lab, expect="7.2.4")
    tab._check.click()
    finish_check(tab, "Check failed")
    assert tab._status.text() == "Check failed - see the log" and "FAILED (version mismatch)" in tab._panel.log_text()
    assert any("device check failed" in t for t, _ in ctx.notified)


def test_check_device_compares_the_file_digest_for_aruba_primary_slot(qapp, track, ctx, tmp_path, ssh_lab):
    src = tmp_path / "fw.swi"
    src.write_bytes(os.urandom(30_000))
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    images = f"ArubaOS-CX Primary Image\nSHA-256  : {digest}\nArubaOS-CX Secondary Image\nSHA-256  : {'0' * 64}\n"
    ssh_lab.commands = lambda c: (images, 0)
    tab = make_push(ctx)
    track(tab)
    checking(tab, ssh_lab, src, vendor="aruba-aos-cx")
    tab._check.click()
    finish_check(tab, "Check passed")
    assert "VERIFIED (hash match)" in tab._panel.log_text() and ssh_lab.executed == ["show images"]
    ssh_lab.commands = lambda c: (images.replace(digest, "f" * 64), 0)  # the primary slot holds something else
    tab._check.click()
    finish_check(tab, "Check failed")
    assert "FAILED (checksum mismatch)" in tab._panel.log_text()


def test_check_device_without_a_hash_command_checks_the_version_and_says_the_file_was_skipped(
    qapp, track, ctx, tmp_path, ssh_lab
):
    src = tmp_path / "fw.out"
    src.write_bytes(b"x" * 100)
    ssh_lab.commands = lambda c: ("Version: FortiGate-60E v7.2.4\n", 0)
    tab = make_push(ctx)
    track(tab)
    checking(tab, ssh_lab, src, vendor="fortinet", expect="7.2.4")  # FortiOS cannot hash a file
    tab._check.click()
    finish_check(tab, "Check passed")
    assert "The file is not compared" in tab._panel.log_text() and ssh_lab.executed == ["get system status"]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("ftp", "runs commands over SSH"),
        ("no_vendor", "Choose a vendor"),
        ("nothing", "Choose a file to compare and/or enter the version"),
        ("cannot_hash", "has no built-in file-hash command"),
        ("no_version_cmd", "No version command is known"),
        ("bad_template", "{nope}"),
        ("no_user", "Enter the username"),
    ],
)
def test_check_device_validation_messages(qapp, track, ctx, tmp_path, ssh_lab, warnings, setup, message):
    src = tmp_path / "fw.bin"
    src.write_bytes(b"x" * 10)
    tab = make_push(ctx)
    track(tab)
    opts = checking(tab, ssh_lab, src, vendor="fortinet")  # a file, FortiOS, no version: cannot hash -> message
    if setup == "ftp":
        opts._protocol.setCurrentIndex(opts._protocol.findData("ftp"))
    elif setup == "no_vendor":
        choose_vendor(opts, "")
        tab._expect.setText("7.2.4")
    elif setup == "nothing":
        tab._file.set_path("")
    elif setup == "no_version_cmd":
        choose_vendor(opts, "sophos-sfos")
        tab._expect.setText("1.0")
    elif setup == "bad_template":
        opts._verify_cmd.setText("md5sum {nope}")
    elif setup == "no_user":
        opts._user.setText("")
    tab._check.click()
    assert warnings and message in warnings[-1], warnings
    assert ssh_lab.executed == [] and tab._check.text() == "Check device"  # nothing started


def test_check_device_can_be_cancelled_and_locks_the_other_actions_meanwhile(qapp, track, ctx, ssh_lab):
    gate = threading.Event()

    def slow(command):
        gate.wait(10)
        return "Version: FortiGate v7.2.4\n", 0

    ssh_lab.commands = slow
    tab = make_push(ctx)
    track(tab)
    try:
        checking(tab, ssh_lab, expect="7.2.4")
        tab._check.click()
        wait_until(lambda: tab._check.text() == "Cancel check" and ssh_lab.executed)
        assert not tab._action.isEnabled() and not tab._host.isEnabled()  # no push can start during a check
        tab._check.click()  # cancel
        finish_check(tab, "Check cancelled")
    finally:
        gate.set()  # let the fake device's worker thread finish
    assert tab._action.isEnabled() and tab._host.isEnabled()


def test_check_device_uses_the_custom_verify_command(qapp, track, ctx, tmp_path, ssh_lab):
    src = tmp_path / "fw.out"
    src.write_bytes(os.urandom(2_000))
    ssh_lab.commands = lambda c: (f"{hashlib.md5(src.read_bytes()).hexdigest()}  fw.out\n", 0)
    tab = make_push(ctx)
    track(tab)
    opts = checking(tab, ssh_lab, src, vendor="fortinet")  # no built-in hash for FortiOS ...
    opts._verify_cmd.setText("md5sum {path}")  # ... so the operator supplies one
    opts._remote.setText("/data/")
    tab._check.click()
    finish_check(tab, "Check passed")
    assert ssh_lab.executed == ["md5sum /data/fw.out"]
