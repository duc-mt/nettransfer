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


def parse_free_bytes(kind: str, output: str) -> Optional[int]:
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
    try:
        output, _ = run_exec(transport, vendor.space_cmd.format(dir=directory), timeout)
    except RemoteError as exc:
        return SpaceResult(False, detail=f"free-space check failed: {exc}")
    free = parse_free_bytes(vendor.space_parser, output)
    if free is None:
        return SpaceResult(False, detail="free-space check skipped: could not read the device's reply")
    return SpaceResult(True, free, f"{free} bytes free on {directory} (need {size})")
