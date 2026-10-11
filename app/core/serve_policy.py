"""Safety policy shared by the TFTP and HTTP(S) servers.

TFTP/HTTP have no authentication, so a forgotten server on a production VLAN is a real risk:
  * allowlist     - only these client IPs/networks may fetch the file
  * stop after 1  - stop serving after the first successful transfer
  * idle timeout  - stop automatically after N seconds without any transfer
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple, Union

from .events import EventSink, Level, LogEvent

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


@dataclass(frozen=True)
class ServePolicy:
    stop_after_first: bool = False
    idle_timeout: Optional[float] = None  # seconds; None = never
    allowed: Tuple[Network, ...] = ()  # empty = anyone


def parse_allowlist(text: str) -> Tuple[Network, ...]:
    nets = []
    for part in re.split(r"[,\s;]+", (text or "").strip()):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(f"'{part}' is not a valid IP address or CIDR network") from None
    return tuple(nets)


class ServeGuard:
    """Runtime enforcement of a ServePolicy. Thread-safe; ``stop`` is the server's stop callback."""

    def __init__(
        self,
        policy: ServePolicy,
        emit: EventSink,
        stop: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy, self._emit, self._stop, self._clock = policy, emit, stop, clock
        self._lock = threading.Lock()
        self._active = 0
        self._last = clock()
        self._stopping = False

    def client_allowed(self, ip: Optional[str]) -> bool:
        if not self._policy.allowed:
            return True
        try:
            addr = ipaddress.ip_address(ip or "")
        except ValueError:
            addr = None
        if addr is not None and any(addr.version == n.version and addr in n for n in self._policy.allowed):
            return True
        self._emit(LogEvent(Level.WARNING, f"{ip or 'unknown'}: request blocked (not in the allowed list)"))
        return False

    def transfer_started(self) -> None:
        with self._lock:
            self._active += 1
            self._last = self._clock()

    def transfer_finished(self, success: bool) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last = self._clock()
            stop_now = success and self._policy.stop_after_first and not self._stopping
            if stop_now:
                self._stopping = True
        if stop_now:
            self._emit(LogEvent(Level.INFO, "Serve-once: transfer complete - stopping the server"))
            self._stop()

    def start_watchdog(self, done: threading.Event, interval: float = 0.5) -> None:
        timeout = self._policy.idle_timeout
        if not timeout:
            return

        def watch() -> None:
            while not done.wait(interval):
                with self._lock:
                    idle = self._active == 0 and self._clock() - self._last >= timeout
                    if idle and self._stopping:
                        return
                    if idle:
                        self._stopping = True
                if idle:
                    self._emit(LogEvent(Level.WARNING, f"Idle for {timeout:g}s - stopping the server"))
                    self._stop()
                    return

        threading.Thread(target=watch, daemon=True, name="serve-idle-watchdog").start()
