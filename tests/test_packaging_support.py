import importlib
import sys
from pathlib import Path

import pytest

from app import crashlog
from app.platform.detect import OSName, PlatformInfo
from app.platform.privileges import BindProbe, relaunch_command, remedies
from app.resources import resource_path

ROOT = Path(__file__).resolve().parent.parent


def test_crashlog_writes_file_and_shows_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    shown = []
    crashlog.install(tmp_path / "data", lambda title, text: shown.append((title, text)))
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    text = (tmp_path / "data" / "crash.log").read_text()
    assert "RuntimeError: boom" in text and "Traceback" in text
    assert shown and "boom" in shown[0][1] and "crash.log" in shown[0][1]


def test_frozen_build_hints_name_the_executable_and_skip_setcap(monkeypatch):
    perm = BindProbe(False, "permission", "denied")
    linux = PlatformInfo(OSName.LINUX, "6", False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/tools/nettransfer")
    assert relaunch_command() == '"/opt/tools/nettransfer"'
    lines = remedies(linux, perm, 69)
    assert any('sudo "/opt/tools/nettransfer"' in line for line in lines)
    assert not any("setcap" in line for line in lines)  # breaks PyInstaller one-file binaries on Linux
    monkeypatch.delattr(sys, "frozen")
    assert relaunch_command() == "python3 run.py"
    assert any("setcap" in line for line in remedies(linux, perm, 69))


def test_windows_hint_includes_firewall_rule_command():
    win = PlatformInfo(OSName.WINDOWS, "11", False)
    text = " ".join(remedies(win, BindProbe(True), 69))
    assert "netsh advfirewall firewall add rule" in text and "localport=69" in text


def test_resource_path_uses_meipass_when_frozen(monkeypatch, tmp_path):
    assert resource_path("assets/icon.png").exists()  # from source
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resource_path("assets/icon.png") == tmp_path / "assets" / "icon.png"


def test_run_py_line_buffering_tolerates_missing_streams(monkeypatch):
    run = importlib.import_module("run")
    monkeypatch.setattr(sys, "stdout", None)  # what a windowed Windows build has
    monkeypatch.setattr(sys, "stderr", None)
    run._line_buffer_output()  # must not raise


def test_icons_exist_for_all_platforms():
    for name in ("icon.png", "icon.ico", "icon.icns"):
        assert (ROOT / "assets" / name).stat().st_size > 1000
    PIL = pytest.importorskip("PIL.Image")
    sizes = PIL.open(ROOT / "assets" / "icon.ico").info.get("sizes", set())
    assert {(16, 16), (32, 32), (256, 256)} <= set(sizes)


def test_spec_and_build_script_are_consistent():
    import ast

    spec = (ROOT / "packaging" / "nettransfer.spec").read_text()
    ast.parse(spec)
    for needle in (
        "run.py",
        "cli_entry.py",
        "icon.icns",
        "icon.ico",
        "NetTransfer.app",
        "NSLocalNetworkUsageDescription",
        "CODESIGN_IDENTITY",
        "console=not IS_WIN",
    ):
        assert needle in spec, needle
    assert "windows-latest" in (ROOT / ".github" / "workflows" / "build.yml").read_text()


def test_workflow_is_valid_yaml_with_all_three_platforms():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text())
    oses = {m["os"] for m in wf["jobs"]["package"]["strategy"]["matrix"]["include"]}
    assert {"windows-latest", "ubuntu-22.04", "macos-latest"} <= oses
    assert any("intel" in o for o in oses)
    assert "needs" in wf["jobs"]["package"] and wf["jobs"]["release"]["needs"] == "package"


def test_gui_selftest_from_source_exits_cleanly():
    """The same check CI runs inside every packaged binary: build the whole GUI offscreen, run the loop, exit 0."""
    import os
    import subprocess

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--selftest"], capture_output=True, text=True, timeout=120, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "selftest ok" in result.stdout
