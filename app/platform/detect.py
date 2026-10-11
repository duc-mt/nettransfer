"""OS detection (Windows / macOS / Linux) and admin-rights check."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from enum import Enum


class OSName(str, Enum):
    WINDOWS = "Windows"
    MACOS = "macOS"
    LINUX = "Linux"
    OTHER = "Other"


@dataclass(frozen=True)
class PlatformInfo:
    os: OSName
    release: str
    is_admin: bool

    @property
    def is_unix(self) -> bool:
        return self.os in (OSName.MACOS, OSName.LINUX)

    def describe(self) -> str:
        role = "administrator" if self.is_admin else "standard user"
        return f"{self.os.value} {self.release} ({role})"


_SYSTEM_TO_OS = {"Windows": OSName.WINDOWS, "Darwin": OSName.MACOS, "Linux": OSName.LINUX}


def detect_platform() -> PlatformInfo:
    os_name = _SYSTEM_TO_OS.get(platform.system(), OSName.OTHER)
    return PlatformInfo(os=os_name, release=platform.release(), is_admin=_is_admin(os_name))


def _is_admin(os_name: OSName) -> bool:
    try:
        if os_name is OSName.WINDOWS:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        return os.geteuid() == 0
    except Exception:
        return False
