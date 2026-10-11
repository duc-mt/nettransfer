"""Locate bundled assets both from source and from a PyInstaller build (sys._MEIPASS)."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative
