"""Port-69 capability probe, per-OS remedies and the startup report.

The probe actually tries to bind: 'root or not' is only a hint (Linux can grant
low ports via capabilities/sysctl, macOS relaxes the rule for some binds).
"""

from __future__ import annotations

import errno
import socket
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .detect import OSName, PlatformInfo


@dataclass(frozen=True)
class BindProbe:
    ok: bool
    reason: str = ""  # "", "permission", "in_use", "address", "other"
    detail: str = ""


def probe_udp_bind(ip: str, port: int) -> BindProbe:
    """Try to bind (and immediately release) a UDP socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, port))
        return BindProbe(True)
    except OSError as exc:
        return BindProbe(False, _classify(exc), str(exc))
    finally:
        sock.close()


def _classify(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission"
    if exc.errno in {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)}:
        return "in_use"
    if exc.errno in {errno.EADDRNOTAVAIL, getattr(errno, "WSAEADDRNOTAVAIL", -1)}:
        return "address"
    return "other"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def relaunch_command() -> str:
    """How the user should re-run this very program (a packaged build has no 'python run.py')."""
    return f'"{sys.executable}"' if is_frozen() else "python3 run.py"


def remedies(info: PlatformInfo, probe: BindProbe, port: int, relaunch: Optional[str] = None) -> List[str]:
    """Plain-text, actionable lines for the banner / console. Empty = nothing to say."""
    relaunch = relaunch or relaunch_command()
    lines: List[str] = []
    if not probe.ok:
        if probe.reason == "permission":
            if info.is_unix:
                lines.append(
                    f"UDP port {port} needs elevated privileges here. Re-run with:  sudo {relaunch}"
                    "   (use 'sudo -E' if the window fails to open)"
                )
                # setcap is skipped for packaged builds: file capabilities make the loader ignore
                # LD_LIBRARY_PATH, which breaks a PyInstaller one-file executable on Linux.
                if info.os is OSName.LINUX and not is_frozen():
                    lines.append(
                        "Or grant the capability once:  sudo setcap 'cap_net_bind_service=+ep' "
                        '"$(readlink -f "$(which python3)")"   (applies to that Python binary)'
                    )
            else:
                lines.append(f"Windows denied UDP port {port}: check security software, or run as Administrator.")
        elif probe.reason == "in_use":
            lines.append(
                f"UDP port {port} is already in use - stop other TFTP servers "
                "(tftpd-hpa, dnsmasq, Tftpd64, SolarWinds ...) first."
            )
        elif probe.reason == "address":
            lines.append("The selected IP is not assigned to any local interface - refresh the interface list.")
        else:
            lines.append(f"Cannot bind UDP port {port}: {probe.detail}")
    if info.os is OSName.WINDOWS:
        lines.append(
            "If Windows Defender Firewall prompts, allow access or devices cannot reach the server. "
            f'Or add the rule once (admin prompt):  netsh advfirewall firewall add rule name="NetTransfer {port}" '
            f"dir=in action=allow protocol=UDP localport={port}"
        )
    return lines


@dataclass(frozen=True)
class StartupReport:
    platform: PlatformInfo
    tftp_port: int
    probe_ip: str
    probe: BindProbe
    messages: List[str] = field(default_factory=list)


def build_startup_report(
    info: PlatformInfo,
    port: int,
    default_ip: Optional[str],
    prober: Callable[[str, int], BindProbe] = probe_udp_bind,
) -> StartupReport:
    ip = default_ip or "0.0.0.0"
    probe = prober(ip, port)
    return StartupReport(info, port, ip, probe, remedies(info, probe, port))
