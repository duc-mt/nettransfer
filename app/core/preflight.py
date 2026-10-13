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


_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
_JUNOS_ROW = re.compile(
    r"^\s*(?P<fs>\S+)\s+(?P<size>[\d.]+[BKMGTP]?)\s+(?P<used>[\d.]+[BKMGTP]?)\s+(?P<avail>[\d.]+[BKMGTP]?)\s+"
    r"(?P<cap>\d+%)\s+(?P<mount>/\S*)\s*$",
    re.MULTILINE,
)


def _human_to_bytes(text: str) -> Optional[int]:
    match = re.fullmatch(r"([\d.]+)([BKMGTP]?)", text)
    if match is None:
        return None
    try:
        return int(float(match.group(1)) * _UNITS[match.group(2)])
    except ValueError:
        return None


def _junos_free(output: str, directory: str) -> Optional[int]:
    """Junos ``show system storage`` is a human-readable df: pick the row whose mount point is the longest prefix of
    the target directory (a relative target lands in the user's home, which lives under /var)."""
    target = directory if directory.startswith("/") else "/var/home"
    best: Optional[re.Match[str]] = None
    for row in _JUNOS_ROW.finditer(output):
        mount = row.group("mount")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and (
            best is None or len(mount) > len(best.group("mount"))
        ):
            best = row
    return _human_to_bytes(best.group("avail")) if best else None


def parse_free_bytes(kind: str, output: str, directory: str = "") -> Optional[int]:
    if kind == "junos-storage":
        return _junos_free(output, directory)
    if kind == "bytes-free":  # Cisco 'dir': "7107084288 bytes total (4947251200 bytes free)"
        match = re.search(r"(\d+)\s+bytes\s+free", output, re.IGNORECASE)
        return int(match.group(1)) if match else None
    if kind == "df":  # df -Pk: Filesystem 1024-blocks Used Available Capacity Mounted
        lines = [line.split() for line in output.strip().splitlines() if line.strip()]
        if len(lines) >= 2 and len(lines[-1]) >= 4 and lines[-1][3].isdigit():
            return int(lines[-1][3]) * 1024
    return None


def check_free_space(
    transport: paramiko.Transport, vendor: Optional[Vendor], device_file: str, size: int, timeout: float = 60.0
) -> SpaceResult:
    if vendor is None or not vendor.space_cmd:
        return SpaceResult(False, detail="free-space check is not available for this vendor")
    directory = parent_dir(device_file)
    if not is_safe_device_path(directory):
        return SpaceResult(False, detail=f"free-space check skipped: unsafe directory '{directory}'")
    free: Optional[int] = None
    try:
        for command in (vendor.space_cmd, vendor.space_fallback):
            if not command:
                continue
            output, _ = run_exec(transport, command.format(dir=directory), timeout)
            free = parse_free_bytes(vendor.space_parser, output, directory)
            if free is not None:
                break
    except RemoteError as exc:
        return SpaceResult(False, detail=f"free-space check failed: {exc}")
    if free is None:
        return SpaceResult(False, detail="free-space check skipped: could not read the device's reply")
    return SpaceResult(True, free, f"{free} bytes free on {directory} (need {size})")
