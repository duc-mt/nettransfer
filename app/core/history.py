"""Transfer history from the audit logs: which device got which file, when, and did it verify?

Parses the human-readable audit lines (``YYYY-MM-DD HH:MM:SS [source] LEVEL message``) into records and renders a
self-contained HTML report (no scripts, no external assets, every value escaped).
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(?P<source>[^\]]*)\] (?P<level>\w+)\s+(?P<msg>.*?)(?:  ~[0-9a-f]{32})?$"
)
_PUSHED = re.compile(r"(?:\[[^\]]+\] )?Pushed (?P<file>.+?) to (?P<host>\S+) via (?P<proto>\w+) in ")
_VERIFIED = re.compile(
    r"(?:\[[^\]]+\] )?Verified (?P<file>.+?) on (?P<host>\S+): (?P<algo>\w+) (?P<digest>[0-9a-f]+) matches"
)
_INSTALLED = re.compile(r"(?:\[[^\]]+\] )?Install plan applied on (?P<host>\S+)")
_SERVING = re.compile(r"(?P<peer>\S+): (?:GET \S+|requested '[^']*') -> serving (?P<file>.+?) \(")  # HTTP and TFTP
_SENT = re.compile(r"(?P<peer>\S+): sent [\d.]+ \S+ \(")
_FAILED = re.compile(r"^\[(?P<host>[^\]]+)\] (?P<why>.+)$")


@dataclass
class Record:
    ts: str
    device: str
    file: str
    via: str  # SCP / SFTP / FTP / FTPS / TFTP / HTTP
    verified: str = ""  # "MD5 abc..." when the device confirmed the digest
    installed: bool = False
    source: str = ""


@dataclass
class History:
    records: List[Record] = field(default_factory=list)
    errors: List[Tuple[str, str, str]] = field(default_factory=list)  # (ts, device, message)

    def latest_per_device(self) -> Dict[str, Record]:
        latest: Dict[str, Record] = {}
        for r in self.records:
            latest[r.device] = r
        return latest

    def filtered(self, device: str = "", file: str = "") -> History:
        keep = [r for r in self.records if device.lower() in r.device.lower() and file.lower() in r.file.lower()]
        errs = [e for e in self.errors if device.lower() in e[1].lower()] if not file else []
        return History(keep, errs)


def parse_lines(lines: Iterable[str], source_name: str = "") -> History:
    history = History()
    pending_file: Dict[str, str] = {}  # server side: peer -> file it last asked for
    open_push: Dict[str, Record] = {}
    for raw in lines:
        m = _LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        ts, source, level, msg = m.group("ts", "source", "level", "msg")
        if level == "ERROR":
            f = _FAILED.match(msg)
            history.errors.append((ts, f.group("host") if f else "", f.group("why") if f else msg))
            continue
        if (p := _PUSHED.search(msg)) is not None:
            rec = Record(ts, p.group("host"), p.group("file"), p.group("proto"), source=source)
            history.records.append(rec)
            open_push[rec.device] = rec
        elif (v := _VERIFIED.search(msg)) is not None:
            rec = open_push.get(v.group("host"))
            if rec is not None:
                rec.verified = f"{v.group('algo')} {v.group('digest')}"
        elif (i := _INSTALLED.search(msg)) is not None:
            rec = open_push.get(i.group("host"))
            if rec is not None:
                rec.installed = True
        elif (s := _SERVING.search(msg)) is not None:
            pending_file[s.group("peer")] = s.group("file")
        elif (d := _SENT.search(msg)) is not None:
            peer = d.group("peer")
            via = "TFTP" if source.upper().startswith("TFTP") else "HTTP" if "HTTP" in source.upper() else source
            history.records.append(Record(ts, peer, pending_file.get(peer, "(file)"), via, source=source))
    return history


def load_history(directory: Path) -> History:
    combined = History()
    for path in sorted(directory.glob("nettransfer-*.log")):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                part = parse_lines(fh, path.name)
        except OSError:
            continue
        combined.records += part.records
        combined.errors += part.errors
    return combined


def render_html(history: History, title: str = "NetTransfer history") -> str:
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(r.ts)}</td><td>{e(r.device)}</td><td>{e(r.file)}</td><td>{e(r.via)}</td>"
        f"<td class={'ok' if r.verified else 'na'}>{e(r.verified) if r.verified else 'not verified'}</td>"
        f"<td>{'yes' if r.installed else ''}</td></tr>"
        for r in reversed(history.records)
    )
    latest = "".join(
        f"<tr><td>{e(device)}</td><td>{e(r.file)}</td><td>{e(r.ts)}</td><td class={'ok' if r.verified else 'na'}>"
        f"{'verified' if r.verified else 'not verified'}</td></tr>"
        for device, r in sorted(history.latest_per_device().items())
    )
    errors = "".join(
        f"<tr><td>{e(ts)}</td><td>{e(device)}</td><td>{e(msg)}</td></tr>"
        for ts, device, msg in reversed(history.errors[-200:])
    )
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{e(title)}</title>
<style>
:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}
body{{margin:2rem auto;max-width:72rem;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 2rem}}
th,td{{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #8884;font-size:.9rem;word-break:break-all}}
th{{font-weight:600}} .ok{{color:#2da44e}} .na{{color:#9a6700}}
</style></head><body>
<h1>{e(title)}</h1>
<p>{len(history.records)} transfers on record, {len(history.errors)} errors. Generated {e(stamp)}.</p>
<h2>Current file per device</h2>
<table><tr><th>Device</th><th>Last file</th><th>When</th><th>Integrity</th></tr>
{latest or "<tr><td colspan=4>none</td></tr>"}</table>
<h2>All transfers (newest first)</h2>
<table><tr><th>When</th><th>Device</th><th>File</th><th>Via</th><th>Integrity</th><th>Install applied</th></tr>
{rows or "<tr><td colspan=6>none</td></tr>"}</table>
<h2>Errors (last 200)</h2>
<table><tr><th>When</th><th>Device</th><th>Message</th></tr>{errors or "<tr><td colspan=3>none</td></tr>"}</table>
</body></html>
"""


def text_summary(history: History) -> str:
    out = []
    for device, r in sorted(history.latest_per_device().items()):
        out.append(f"{device:<20} {r.file:<32} {r.ts}  {r.via:<5} {'verified' if r.verified else 'unverified'}")
    return "\n".join(out) or "no transfers recorded"


def optional_ts(history: History) -> Optional[str]:
    return history.records[-1].ts if history.records else None
