"""Trust-on-first-use SSH host key store (OpenSSH known_hosts format, our own file)."""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from pathlib import Path
from typing import Callable, Optional

import paramiko

NEW, MATCH, CHANGED = "new", "match", "changed"


class KnownHostsStore:
    """``path=None`` keeps everything in memory (tests, or when no data dir is writable)."""

    def __init__(self, path: Optional[Path] = None, fixup: Optional[Callable[[Path], None]] = None) -> None:
        self._path, self._fixup = path, fixup
        self._memory = paramiko.HostKeys()
        self._lock = threading.Lock()

    @staticmethod
    def host_id(host: str, port: int) -> str:
        return host if port == 22 else f"[{host}]:{port}"

    @staticmethod
    def fingerprint(key: paramiko.PKey) -> str:
        return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")

    def check(self, host: str, port: int, key: paramiko.PKey) -> str:
        with self._lock:
            entry = self._load().lookup(self.host_id(host, port))
            known = entry.get(key.get_name()) if entry is not None else None
            if known is None:
                return NEW
            return MATCH if known.asbytes() == key.asbytes() else CHANGED

    def trust(self, host: str, port: int, key: paramiko.PKey) -> None:
        with self._lock:
            keys = self._load()
            keys.add(self.host_id(host, port), key.get_name(), key)  # replaces a same-type entry
            if self._path is None:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                keys.save(str(self._path))
                os.chmod(self._path, 0o600)
                if self._fixup:
                    self._fixup(self._path)
            except OSError:
                self._memory = keys  # unwritable: still remember for this session

    def _load(self) -> paramiko.HostKeys:
        if self._path is None:
            return self._memory
        keys = paramiko.HostKeys()
        if self._path.exists():
            try:
                keys.load(str(self._path))
            except OSError:
                return self._memory
        return keys
