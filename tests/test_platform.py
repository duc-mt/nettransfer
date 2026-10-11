import socket

from app.platform.detect import OSName, PlatformInfo, detect_platform
from app.platform.network import list_interfaces
from app.platform.privileges import BindProbe, build_startup_report, probe_udp_bind, remedies


def test_detect_platform_returns_known_os():
    info = detect_platform()
    assert info.os in set(OSName)
    assert info.describe()


def test_probe_ok_and_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        probe = probe_udp_bind("127.0.0.1", port)
        assert not probe.ok and probe.reason == "in_use"
    assert probe_udp_bind("127.0.0.1", port).ok  # released again


def test_probe_bad_address():
    probe = probe_udp_bind("203.0.113.77", 6969)
    assert not probe.ok and probe.reason == "address"


def test_remedies_per_os():
    perm = BindProbe(False, "permission", "denied")
    linux = PlatformInfo(OSName.LINUX, "6.1", False)
    mac = PlatformInfo(OSName.MACOS, "14", False)
    win = PlatformInfo(OSName.WINDOWS, "11", False)
    assert any("sudo python3 run.py" in line for line in remedies(linux, perm, 69))
    assert any("setcap" in line for line in remedies(linux, perm, 69))
    assert not any("setcap" in line for line in remedies(mac, perm, 69))
    assert any("Firewall" in line for line in remedies(win, BindProbe(True), 69))
    assert remedies(linux, BindProbe(True), 69) == []


def test_startup_report_uses_injected_prober():
    info = PlatformInfo(OSName.LINUX, "6.1", False)
    report = build_startup_report(info, 69, "10.0.0.5", prober=lambda ip, port: BindProbe(False, "permission", "x"))
    assert report.probe_ip == "10.0.0.5" and not report.probe.ok and report.messages


def test_list_interfaces_has_no_loopback():
    for iface in list_interfaces():
        assert not iface.ip.startswith("127.")
