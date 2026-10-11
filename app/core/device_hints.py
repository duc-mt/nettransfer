"""Copy-ready device commands and remote-path presets.

These are *templates*: filesystem names, VRFs and syntax vary by platform and release, so the UI
presents them as starting points to adjust, not as guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Hint:
    vendor: str
    pull: str  # placeholders: {scheme} {ip} {port} {file}
    verify: Optional[str] = None  # command to verify the copied file, if there is a standard one


TFTP_HINTS: Tuple[Hint, ...] = (
    Hint("Cisco IOS / IOS-XE", "copy tftp://{ip}/{file} flash:{file}", "verify /md5 flash:{file}"),
    Hint("Cisco NX-OS", "copy tftp://{ip}/{file} bootflash:{file} vrf management", "show file bootflash:{file} md5sum"),
    Hint("Huawei VRP", "tftp {ip} get {file}"),
    Hint("Fortinet FortiGate", "execute restore image tftp {file} {ip}"),
)

HTTP_HINTS: Tuple[Hint, ...] = (
    Hint("Cisco IOS / IOS-XE", "copy {scheme}://{ip}:{port}/{file} flash:{file}", "verify /md5 flash:{file}"),
    Hint(
        "Cisco NX-OS",
        "copy {scheme}://{ip}:{port}/{file} bootflash:{file} vrf management",
        "show file bootflash:{file} md5sum",
    ),
    Hint(
        "Juniper Junos", "file copy {scheme}://{ip}:{port}/{file} /var/tmp/{file}", "file checksum md5 /var/tmp/{file}"
    ),
)

# Starting points for the SCP/SFTP remote path field
REMOTE_PATH_PRESETS: Dict[str, str] = {
    "(device default)": "",
    "Cisco IOS / IOS-XE": "flash:",
    "Cisco NX-OS": "bootflash:",
    "Juniper Junos": "/var/tmp/",
    "Linux / generic": "/tmp/",
}


def render(template: str, **values: object) -> str:
    return template.format(**values)
