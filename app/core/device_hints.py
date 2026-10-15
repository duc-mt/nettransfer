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
    note: str = ""  # caveat shown next to the command (e.g. "needs an SSH server of your own")


TFTP_HINTS: Tuple[Hint, ...] = (
    Hint("Cisco IOS / IOS-XE", "copy tftp://{ip}/{file} flash:{file}", "verify /md5 flash:{file}"),
    Hint("Cisco NX-OS", "copy tftp://{ip}/{file} bootflash:{file} vrf management", "show file bootflash:{file} md5sum"),
    Hint("Arista EOS", "copy tftp://{ip}/{file} flash:", "verify /sha256 flash:{file}"),
    Hint(
        "Aruba AOS-CX",
        "copy tftp://{ip}/{file} primary",
        "show images",
        note="Add 'vrf mgmt' if the server is reached through the management VRF. Then 'boot set-default primary' and "
        "'boot system primary'.",
    ),
    Hint(
        "Huawei VRP",
        "tftp {ip} get {file}",
        "check system-software flash:/{file}",
        note="Answer Y to the check prompt; current releases print a SHA256. Then 'startup system-software "
        "flash:/FILE' and 'reboot'.",
    ),
    Hint(
        "Fortinet FortiGate",
        "execute restore image tftp {file} {ip}",
        "get system status",
        note="The FortiGate checks the image, installs it and restarts by itself. FortiOS cannot hash a file: confirm "
        "the version afterwards.",
    ),
    Hint(
        "Ruijie RGOS (12.x)",
        "upgrade download tftp://{ip}/{file}",
        "show version",
        note="Takes effect after 'reload'. RGOS 10.x instead: 'copy tftp://SERVER/FILE flash:rgos.bin'.",
    ),
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
    Hint("Arista EOS", "copy {scheme}://{ip}:{port}/{file} flash:", "verify /sha256 flash:{file}"),
    Hint(
        "MikroTik RouterOS",
        '/tool fetch url="{scheme}://{ip}:{port}/{file}" mode={scheme}',
        note="With HTTPS and the self-signed certificate add check-certificate=no (or install the certificate).",
    ),
    Hint(
        "VyOS",
        "add system image {scheme}://{ip}:{port}/{file}",
        "show system image",
        note="Interactive: the installer asks a few questions (image name, default boot image, copy config).",
    ),
    Hint(
        "pfSense / OPNsense (FreeBSD)",
        "fetch -o /tmp/{file} {scheme}://{ip}:{port}/{file}",
        "sha256 /tmp/{file}",
        note="Run from a shell (root, or menu option 8). HTTPS with the self-signed certificate: add --no-verify-peer.",
    ),
    Hint(
        "Check Point Gaia (Expert)",
        "curl_cli -k -o /var/log/{file} {scheme}://{ip}:{port}/{file}",
        "sha256sum /var/log/{file}",
        note="Run in Expert mode (bash); -k skips verification of the self-signed certificate. Then "
        'clish -c "installer import local /var/log/FILE".',
    ),
)

_SSH_NOTE = (
    "The device pulls from an SSH server that must run on this machine - NetTransfer serves TFTP and HTTP(S) only. "
    "Replace USER and adjust the path to where the file really is."
)
# Commands where the device pulls over SSH from a server you run yourself (shown in the TFTP and HTTP tabs too)
SSH_PULL_HINTS: Tuple[Hint, ...] = (
    Hint("Aruba AOS-CX (SFTP pull)", "copy sftp://USER@{ip}/{file} primary", "show images", note=_SSH_NOTE),
    Hint(
        "Palo Alto PAN-OS (SCP pull)",
        "scp import software from USER@{ip}:{file}",
        "request system software info",
        note=_SSH_NOTE + " Then 'request system software install version VERSION' (a background job: watch "
        "'show jobs all') and 'request restart system'. PAN-OS has no checksum command.",
    ),
)

# Starting points for the SCP/SFTP remote path field
REMOTE_PATH_PRESETS: Dict[str, str] = {
    "(device default)": "",
    "Cisco IOS / IOS-XE": "flash:",
    "Cisco NX-OS": "bootflash:",
    "Arista EOS": "flash:",
    "Huawei VRP": "flash:/",
    "Juniper Junos": "/var/tmp/",
    "pfSense / OPNsense": "/tmp/",
    "Check Point Gaia": "/var/log/",
    "Linux / generic": "/tmp/",
}


def render(template: str, **values: object) -> str:
    return template.format(**values)


def hints_for(protocol: str) -> Tuple[Hint, ...]:
    """Every command template that applies when serving ``protocol`` (tftp, http or https)."""
    base = TFTP_HINTS if protocol == "tftp" else HTTP_HINTS
    return base + SSH_PULL_HINTS


def hint_slug(hint: Hint) -> str:
    return "".join(c if c.isalnum() else "-" for c in hint.vendor.lower()).strip("-")


def find_hints(protocol: str, query: str = "") -> Tuple[Hint, ...]:
    """Hints whose vendor name contains ``query`` (case-insensitive, punctuation-insensitive); all when blank."""
    needle = "".join(c for c in query.lower() if c.isalnum())
    return tuple(h for h in hints_for(protocol) if needle in "".join(c for c in h.vendor.lower() if c.isalnum()))


def render_hint(hint: Hint, protocol: str, ip: str, port: object, file: str) -> Tuple[str, Optional[str]]:
    """(pull command, verify command or None) for one vendor, filled in with the server address and file name."""
    values = {"scheme": protocol, "ip": ip, "port": port, "file": file}
    return render(hint.pull, **values), (render(hint.verify, **values) if hint.verify else None)
