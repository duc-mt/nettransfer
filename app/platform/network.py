"""Local IPv4 discovery for the TFTP server's bind address."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Interface:
    name: str
    ip: str
    is_default: bool = False

    @property
    def label(self) -> str:
        return f"{self.ip}  ({self.name})" + ("  - default route" if self.is_default else "")


def default_route_ip() -> Optional[str]:
    """IP the OS would use to reach the outside world (no packet is sent).

    Returns None on hosts without a route, e.g. a laptop cabled straight to a
    switch with a static address - list_interfaces() still finds those.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return s.getsockname()[0]
    except OSError:
        return None


def list_interfaces() -> List[Interface]:
    """All up, non-loopback IPv4 addresses; the default-route one comes first."""
    default_ip = default_route_ip()
    found: List[Interface] = []
    try:
        import psutil

        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    found.append(Interface(name, addr.address, addr.address == default_ip))
    except Exception:
        found = []
    if not found:
        ips = []
        try:
            ips = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError:
            pass
        if default_ip and default_ip not in ips:
            ips.insert(0, default_ip)
        found = [Interface("local", ip, ip == default_ip) for ip in ips if not ip.startswith("127.")]
    found.sort(key=lambda i: not i.is_default)
    return found
