"""Per-device pre-flight checks before a big push: does the image look right for the vendor, is there room for it?"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import paramiko

from .remote import RemoteError, run_exec
from .vendors import Vendor, is_safe_device_path, parent_dir


@dataclass(frozen=True)
class SpaceResult:
    known: bool  # False = could not be determined (never a reason to block)
    free: int = 0
    detail: str = ""


def extension_warning(vendor: Optional[Vendor], name: str) -> Optional[str]:
    if vendor is None or not vendor.extensions:
        return None
    if name.lower().endswith(vendor.extensions):
        return None
    return f"'{name}' does not look like a {vendor.label} image (expected {' / '.join(vendor.extensions)})"


_UNITS = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2, "G": 1024**3, "GB": 1024**3}
_UNITS.update({"T": 1024**4, "TB": 1024**4, "P": 1024**5, "BYTE": 1, "BYTES": 1})
_DF_HUMAN_ROW = re.compile(
    r"^\s*(?P<fs>\S+)\s+(?P<size>[\d.]+[BKMGTP]?)\s+(?P<used>[\d.]+[BKMGTP]?)\s+(?P<avail>[\d.]+[BKMGTP]?)\s+"
    r"(?P<cap>\d+%)\s+(?P<mount>/\S*)\s*$",
    re.MULTILINE,
)
# Cisco "122185728 bytes total (21233664 bytes free)"; Huawei/H3C "1,090,732 KB total (784,464 KB free)"
_TOTAL_FREE = re.compile(r"\(\s*(?P<n>[\d,]+(?:\.\d+)?)\s*(?P<u>bytes?|KB|MB|GB)\s+free\s*\)", re.IGNORECASE)
_BYTES_FREE = re.compile(r"(\d+)\s+bytes\s+free", re.IGNORECASE)
_FORTIOS_MEMORY = re.compile(r"Memory:\s*\d+k\s+total,.*?(\d+)k\s+free", re.IGNORECASE | re.DOTALL)


def _to_bytes(number: str, unit: str) -> Optional[int]:
    factor = _UNITS.get(unit.upper())
    if factor is None:
        return None
    try:
        return int(float(number.replace(",", "")) * factor)
    except ValueError:
        return None


def _human_to_bytes(text: str) -> Optional[int]:
    match = re.fullmatch(r"([\d.]+)([BKMGTP]?)", text)
    return _to_bytes(match.group(1), match.group(2)) if match else None


def _df_human_free(output: str, directory: str) -> Optional[int]:
    """A human-readable df table (Junos ``show system storage``, PAN-OS ``show system disk-space``): pick the row whose
    mount point is the longest prefix of the target directory (a relative target lands in the user's home, which
    lives under /var)."""
    target = directory if directory.startswith("/") else "/var/home"
    best: Optional[re.Match[str]] = None
    for row in _DF_HUMAN_ROW.finditer(output):
        mount = row.group("mount")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and (
            best is None or len(mount) > len(best.group("mount"))
        ):
            best = row
    return _human_to_bytes(best.group("avail")) if best else None


def _total_free(output: str) -> Optional[int]:
    match = _TOTAL_FREE.search(output)
    return _to_bytes(match.group("n"), match.group("u")) if match else None


def _df_posix(output: str) -> Optional[int]:
    lines = [line.split() for line in output.strip().splitlines() if line.strip()]
    if len(lines) >= 2 and len(lines[-1]) >= 4 and lines[-1][3].isdigit():
        return int(lines[-1][3]) * 1024
    return None


def parse_free_bytes(kind: str, output: str, directory: str = "") -> Optional[int]:
    """Free bytes from a device reply, or None when it cannot be read.

    kinds: ``bytes-free`` (Cisco/Arista ``dir``), ``kb-total-free`` (Huawei/H3C ``dir``: ``N KB total (M KB free)``),
    ``df`` (POSIX/FreeBSD ``df -Pk``), ``df-human`` (Junos/PAN-OS tables with K/M/G sizes), ``fortios-memory``
    (``get system performance status``) and ``auto`` (any figure with an explicit unit, else df)."""
    if kind in ("df-human", "junos-storage"):
        return _df_human_free(output, directory)
    if kind == "kb-total-free":
        return _total_free(output)
    if kind == "bytes-free":
        match = _BYTES_FREE.search(output)
        return int(match.group(1)) if match else None
    if kind == "df":
        return _df_posix(output)
    if kind == "fortios-memory":
        match = _FORTIOS_MEMORY.search(output)
        return int(match.group(1)) * 1024 if match else None
    if kind == "auto":
        for parser in (_total_free, lambda text: parse_free_bytes("bytes-free", text), _df_posix):
            found = parser(output)
            if found is not None:
                return found
    return None


def check_free_space(
    transport: paramiko.Transport, vendor: Optional[Vendor], device_file: str, size: int, timeout: float = 60.0
) -> SpaceResult:
    if vendor is None or not vendor.space_cmd:
        label = vendor.label if vendor else "this device"
        return SpaceResult(False, detail=f"free-space check is not available for {label}")
    directory = vendor.space_dir or parent_dir(device_file)
    if not is_safe_device_path(directory):
        return SpaceResult(False, detail=f"free-space check skipped: unsafe directory '{directory}'")
    free: Optional[int] = None
    last_output = ""
    try:
        for command in (vendor.space_cmd, vendor.space_fallback):
            if not command:
                continue
            last_output, _ = run_exec(transport, command.format(dir=directory), timeout)
            free = parse_free_bytes(vendor.space_parser, last_output, directory)
            if free is not None:
                break
    except RemoteError as exc:
        return SpaceResult(False, detail=f"free-space check failed: {exc}")
    if free is None:
        detail = "free-space check skipped: could not read the device's reply"
        if vendor.shell_hint_pattern and re.search(vendor.shell_hint_pattern, last_output):
            detail += f" - {vendor.shell_hint}"
        return SpaceResult(False, detail=detail)
    return SpaceResult(True, free, f"{free} bytes free on {directory} (need {size})")
