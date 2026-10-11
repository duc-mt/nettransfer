"""Per-user data directory, resolved for the *invoking* user even when run via sudo.

TFTP port 69 needs root on Linux/macOS, but known_hosts, settings and logs belong to the
person who ran ``sudo``, not to root - otherwise they end up root-owned and unreadable later.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def _sudo_user():
    if os.name != "posix" or os.geteuid() != 0:
        return None
    name = os.environ.get("SUDO_USER")
    if not name or name == "root":
        return None
    try:
        import pwd

        return pwd.getpwnam(name)
    except (ImportError, KeyError):
        return None


def user_home() -> Path:
    pw = _sudo_user()
    return Path(pw.pw_dir) if pw else Path.home()


def data_dir() -> Path:
    home = user_home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "NetTransfer"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "NetTransfer"
    xdg = None if _sudo_user() else os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg or home / ".config") / "nettransfer"


def fix_owner(path: Path) -> None:
    """Hand a file/dir created under sudo back to the invoking user (best effort)."""
    pw = _sudo_user()
    if pw is not None:
        try:
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def ensure_dir(path: Path) -> Path:
    missing = []
    probe: Optional[Path] = path
    while probe is not None and not probe.exists():
        missing.append(probe)
        probe = probe.parent if probe.parent != probe else None
    path.mkdir(parents=True, exist_ok=True)
    for created in missing:
        fix_owner(created)
    return path
