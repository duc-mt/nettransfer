"""Per-vendor knowledge used by the post-push features (preflight, verify, install).

Every command here is a *template*: filesystem names, syntax and prompts differ between platforms and releases, so these
are sensible starting points, not guarantees. Anything a profile does not know can be overridden on the command line
(``--verify-cmd``). Nothing here is ever sent to a device unless the operator asked for the matching feature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


class UnknownVendorError(ValueError):
    pass


@dataclass(frozen=True)
class Vendor:
    id: str
    label: str
    default_dir: str = ""  # where a blank remote path is assumed to land (used to build device paths)
    verify_cmd: Optional[str] = None  # placeholders: {path}
    verify_algo: str = "md5"
    space_cmd: Optional[str] = None  # placeholders: {dir}
    space_parser: str = ""  # "bytes-free" (Cisco dir) | "df" (POSIX df -Pk)
    extensions: Tuple[str, ...] = ()  # file extensions a software image normally has; () = do not check
    install: Tuple[str, ...] = ()  # lines typed into an interactive session; placeholders: {path} {file}
    reload: Tuple[str, ...] = ()  # appended only when a reload was explicitly requested ("" = just press Enter)


VENDORS: Dict[str, Vendor] = {
    v.id: v
    for v in (
        Vendor(
            "cisco-ios",
            "Cisco IOS / IOS-XE (bundle mode)",
            default_dir="flash:",
            verify_cmd="verify /md5 {path}",
            space_cmd="dir {dir}",
            space_parser="bytes-free",
            extensions=(".bin", ".tar", ".pkg"),
            install=("configure terminal", "no boot system", "boot system {path}", "end", "write memory"),
            reload=("reload", ""),
        ),
        Vendor(
            "cisco-nxos",
            "Cisco NX-OS",
            default_dir="bootflash:",
            verify_cmd="show file {path} md5sum",
            space_cmd="dir {dir}",
            space_parser="bytes-free",
            extensions=(".bin",),
            install=("configure terminal", "boot nxos {path}", "end", "copy running-config startup-config"),
            reload=("reload", "y"),
        ),
        Vendor(
            "junos",
            "Juniper Junos",
            default_dir="",
            verify_cmd="file checksum md5 {path}",
            extensions=(".tgz", ".tar.gz"),
            install=("request system software add {path}",),
            reload=("request system reboot", "yes"),
        ),
        Vendor(
            "huawei",
            "Huawei VRP",
            default_dir="flash:/",
            extensions=(".cc", ".pat", ".zip"),
            install=("startup system-software {file}",),
            reload=("reboot", "y"),
        ),
        Vendor("fortinet", "Fortinet FortiGate", extensions=(".out",)),
        Vendor(
            "linux",
            "Linux / generic (POSIX shell)",
            verify_cmd="sha256sum {path}",
            verify_algo="sha256",
            space_cmd="df -Pk {dir}",
            space_parser="df",
        ),
    )
}

ALIASES = {"cisco": "cisco-ios", "ios": "cisco-ios", "iosxe": "cisco-ios", "nxos": "cisco-nxos", "junipper": "junos"}


def get_vendor(vendor_id: str) -> Vendor:
    key = (vendor_id or "").strip().lower()
    key = ALIASES.get(key, key)
    try:
        return VENDORS[key]
    except KeyError:
        raise UnknownVendorError(f"unknown vendor '{vendor_id}' (known: {', '.join(sorted(VENDORS))})") from None


# Device command lines are built from file names, so anything outside this set is refused rather than escaped: a
# file called "x; reload" must never become two commands on a router.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/:@%+=,~-]+$")


def is_safe_device_path(path: str) -> bool:
    return bool(_SAFE_PATH.match(path))


def device_path(remote_path: str, name: str, vendor: Optional[Vendor]) -> str:
    """Where the pushed file ends up. A remote path ending in ``/`` or ``:`` is a directory; anything else is taken
    as the full target file name. Blank means the vendor's usual default directory."""
    remote = (remote_path or "").strip()
    if remote in ("", "."):
        return (vendor.default_dir if vendor else "") + name
    if remote.endswith(("/", ":")):
        return remote + name
    return remote


def parent_dir(path: str) -> str:
    """Directory part of a device path: ``flash:img.bin`` -> ``flash:``, ``/var/tmp/x`` -> ``/var/tmp``."""
    cut = max(path.rfind("/"), path.rfind(":"))
    if cut < 0:
        return "."
    return path[: cut + 1] if path[cut] == ":" or cut == 0 else path[:cut]
