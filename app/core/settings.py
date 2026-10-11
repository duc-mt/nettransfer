"""Tiny JSON settings store. Never put passwords or key passphrases in here."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class SettingsStore:
    def __init__(self, path: Optional[Path] = None, fixup: Optional[Callable[[Path], None]] = None) -> None:
        self._path, self._fixup = path, fixup
        self._data: Dict[str, Any] = {}
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, ValueError):
                self._data = {}  # corrupt file: start fresh rather than crash

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
            if self._fixup:
                self._fixup(self._path)
        except OSError:
            pass  # settings are a convenience, never a reason to fail
