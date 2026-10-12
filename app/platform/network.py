"""Local IPv4 discovery for the TFTP server's bind address."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Interface:
    name: str
    ip: str
    is_default: bool = False
    family: int = 4  # 4 or 6

    @property
    def label(self) -> str:
        return (
            f"{self.ip}  ({self.name})"
            + ("  - default route" if self.is_default else "")
            + ("  - IPv6" if self.family == 6 else "")
        )


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


def ip_for_target(target: str) -> Optional[str]:
    """Local address the OS would use to reach ``target`` (IPv4 or IPv6) - i.e. the NIC facing that device's subnet.
    No packet is sent: a UDP "connect" only consults the routing table."""
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(target.split("%")[0]).version == 6 else socket.AF_INET
    except ValueError:
        return None
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect((target, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def interface_for_target(target: str, interfaces: List[Interface]) -> Optional[Interface]:
    ip = ip_for_target(target)
    return next((i for i in interfaces if i.ip == ip), None) if ip else None


def _usable_ipv6(address: str) -> bool:
    try:
        addr = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    return addr.version == 6 and not (addr.is_loopback or addr.is_link_local or addr.is_multicast)


def list_interfaces(include_ipv6: bool = False) -> List[Interface]:
    """All up, non-loopback IPv4 addresses (plus global/ULA IPv6 when asked); the default-route one comes first."""
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
                elif include_ipv6 and addr.family == socket.AF_INET6 and _usable_ipv6(addr.address):
                    found.append(Interface(name, addr.address.split("%")[0], False, 6))
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
