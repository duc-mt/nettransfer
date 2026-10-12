"""Append-only audit trail (one file per day) for change tickets. Never receives secrets.

Every line ends with a chain tag: ``<line>  ~<32 hex>`` where the tag is SHA-256 (or HMAC-SHA-256 when a key is
configured) over the previous line's tag plus this line. Editing, deleting or re-ordering a line breaks every tag after
it, which ``verify_chain`` / ``nettransfer audit verify`` reports.

What this does and does not give you: it is *tamper-evident*, not tamper-proof. Someone who can read the key (it lives
next to the logs by default) can rebuild a consistent chain, and the tail of a file can be cut off without breaking
what remains. Anchor the final tag ("head") somewhere else - the change ticket, a syslog server - to close both gaps.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .events import LogEvent

GENESIS = "0" * 32
_TAG = re.compile(r"^(?P<body>.*)  ~(?P<tag>[0-9a-f]{32})$")


def chain_tag(previous: str, body: str, key: Optional[bytes] = None) -> str:
    data = f"{previous}\n{body}".encode()
    digest = hmac.new(key, data, hashlib.sha256) if key else hashlib.sha256(data)
    return digest.hexdigest()[:32]


def load_or_create_key(path: Path, fixup: Optional[Callable[[Path], None]] = None) -> Optional[bytes]:
    """Random 32-byte HMAC key kept in a 0600 file; None if it cannot be read or created (plain SHA-256 chain)."""
    try:
        if path.exists():
            data = path.read_bytes()
            return data if len(data) >= 16 else None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        if fixup:
            fixup(path)
        return key
    except OSError:
        return None


class AuditLog:
    def __init__(
        self,
        directory: Optional[Path],
        fixup: Optional[Callable[[Path], None]] = None,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        key: Optional[bytes] = None,
    ) -> None:
        self._dir, self._fixup, self._clock, self._key = directory, fixup, clock, key
        self.enabled = enabled
        self._lock = threading.Lock()
        self._head: Dict[Path, str] = {}

    @property
    def path(self) -> Optional[Path]:
        if self._dir is None:
            return None
        return self._dir / f"nettransfer-{time.strftime('%Y%m%d', time.localtime(self._clock()))}.log"

    def write(self, source: str, event: LogEvent) -> None:
        path = self.path
        if not self.enabled or path is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts))
        message = " ".join(event.message.splitlines())
        body = f"{stamp} [{source}] {event.level.value.upper():<7} {message}"
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                new = not path.exists()
                previous = self._head.get(path) or last_tag(path) or GENESIS
                tag = chain_tag(previous, body, self._key)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{body}  ~{tag}\n")
                self._head[path] = tag
                if new and self._fixup:
                    self._fixup(path)
            except OSError:
                pass


def last_tag(path: Path) -> Optional[str]:
    """Chain tag of the last line of an existing file (read from the end, so large logs stay cheap)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            match = _TAG.match(line)
            return match.group("tag") if match else None
    return None


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    lines: int  # non-empty lines inspected
    chained: int  # lines carrying a valid link
    legacy: int  # lines written before chaining existed (not covered)
    head: str  # last tag in the file ("" if none)
    problem: str = ""  # first thing wrong, with its line number


def verify_chain(path: Path, key: Optional[bytes] = None) -> ChainReport:
    previous, chained, legacy, total = GENESIS, 0, 0, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ChainReport(False, 0, 0, 0, "", str(exc))
    problems: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        total += 1
        match = _TAG.match(line)
        if not match:
            if chained:
                problems.append(f"line {number}: no chain tag after the chain started (inserted or edited line)")
                break
            legacy += 1
            continue
        if chain_tag(previous, match.group("body"), key) != match.group("tag"):
            hint = "" if key else " (if the log is HMAC-protected, pass its key)"
            problems.append(f"line {number}: chain broken - this line or an earlier one was changed or removed{hint}")
            break
        previous, chained = match.group("tag"), chained + 1
    head = previous if chained else ""
    return ChainReport(not problems, total, chained, legacy, head, problems[0] if problems else "")
