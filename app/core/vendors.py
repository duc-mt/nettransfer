"""Per-vendor knowledge used by the post-push features (preflight, verify, install, version check).

Every command here is a *template*: filesystem names, syntax and prompts differ between platforms and releases, so these
are sensible starting points, not guarantees. Nothing here is ever sent to a device unless the operator asked for the
matching feature, and anything a profile does not know can be overridden (``--verify-cmd``).

Line conventions in ``install`` / ``reload`` / ``reload_in`` / ``verify_steps`` (typed into an interactive session):

* ``"cmd {path}"``   a command; ``{path}`` is the file on the device, ``{file}`` its name, ``{version}`` the software
  version read from the file name, ``{minutes}`` / ``{hhmm}`` the reload delay;
* ``"@y"``           answer ``y`` **only if the device asked a question**, so a POSIX shell never runs a stray ``yes``;
* ``"@"``            the same with Enter (accept the default);
* ``"@~regex~n"``    answer only to a question matching the regex; ``"@~save~!"`` aborts instead of answering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

TRANSFER_PUSH = "push"  # the device accepts a file over SCP/SFTP/FTP
TRANSFER_PULL = "pull"  # the device must fetch the file itself (serve it, then run the pull command on the device)
TRANSFER_WEB = "web"  # firmware is installed through a web interface only

SHELL_CLI = "cli"  # exec channel commands are interpreted by the vendor CLI
SHELL_POSIX = "posix"  # exec channel commands run in a POSIX shell (VyOS, FreeBSD, Gaia expert/bash, ...)


class UnknownVendorError(ValueError):
    pass


@dataclass(frozen=True)
class Vendor:
    id: str
    label: str
    default_dir: str = ""  # where a blank remote path is assumed to land (used to build device paths)
    # --- integrity check -------------------------------------------------------------------------------------------
    verify_cmd: Optional[str] = None  # run on an exec channel; placeholders: {path} {file}
    verify_steps: Tuple[str, ...] = ()  # instead: typed into an interactive session (for commands that ask first)
    verify_algo: str = "md5"
    verify_marker: str = ""  # regex: use the first digest AFTER this text (a reply listing several images)
    version_cmd: Optional[str] = None  # prints the running software version (``verify --expect-version``)
    version_interactive: bool = False  # version_cmd needs an interactive session (VyOS op-mode)
    # --- storage pre-flight ----------------------------------------------------------------------------------------
    space_cmd: Optional[str] = None  # placeholders: {dir}
    space_fallback: Optional[str] = None  # tried when space_cmd's reply cannot be parsed
    space_parser: str = ""  # see preflight.parse_free_bytes
    space_dir: str = ""  # check this directory instead of the target's parent (e.g. PAN-OS /opt/panrepo)
    space_advisory: bool = False  # too little space only warns (the figure is a proxy, not the real target)
    extensions: Tuple[str, ...] = ()  # file extensions a software image normally has; () = do not check
    # --- post-push actions -----------------------------------------------------------------------------------------
    install: Tuple[str, ...] = ()
    install_label: str = "set the boot image to the pushed file"
    reload: Tuple[str, ...] = ()  # reload now
    reload_in: Tuple[str, ...] = ()  # scheduled reload; () = not supported
    # --- how the device behaves ------------------------------------------------------------------------------------
    transfer: str = TRANSFER_PUSH
    shell: str = SHELL_CLI
    shell_hint_pattern: str = ""  # regex on a reply that shows the login shell is not what the commands need
    shell_hint: str = ""  # what to do about it
    note: str = ""  # shown as a warning before a push to a vendor that normally does not take one


_DF: Dict[str, Any] = dict(space_cmd="df -Pk {dir}", space_parser="df", shell=SHELL_POSIX)  # POSIX / FreeBSD df

_CLISH_HINT = (
    "the login shell is Gaia Clish (restricted): run 'set user USER shell /bin/bash' then 'save config' "
    "(or 'bashUser on' on Spark appliances) so SSH commands run in the Expert (bash) shell, then retry"
)

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
            reload=("reload", "@~save~!", "@"),
            reload_in=("reload in {minutes}", "@~save~!", "@"),
            version_cmd="show version",
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
            reload=("reload", "@y"),
            version_cmd="show version",
        ),
        Vendor(
            "arista-eos",
            "Arista EOS",
            default_dir="flash:",
            verify_cmd="verify /sha256 {path}",
            verify_algo="sha256",
            space_cmd="dir {dir}",
            space_parser="bytes-free",
            extensions=(".swi", ".swix"),
            install=("configure", "boot system {path}", "end", "write memory"),
            reload=("reload", "@~save~!", "@"),
            reload_in=("reload in {hhmm}", "@~save~!", "@"),
            version_cmd="show version",
        ),
        Vendor(
            "junos",
            "Juniper Junos",
            verify_cmd="file checksum md5 {path}",
            space_cmd="show system storage /var",
            space_fallback="show system storage",
            space_parser="df-human",
            extensions=(".tgz", ".tar.gz"),
            install=("request system software add {path}",),
            reload=("request system reboot", "@yes"),
            reload_in=("request system reboot in {minutes}", "@yes"),
            version_cmd="show version",
        ),
        Vendor(
            "huawei",
            "Huawei VRP",
            default_dir="flash:/",
            # 'check system-software' asks "Continue? [Y/N]" first and, on current VRP releases, prints a SHA256 hash.
            # Which bytes it hashes can differ by release: validate once on your device before trusting a mismatch.
            verify_steps=("check system-software {path}", "@y"),
            verify_algo="sha256",
            space_cmd="dir {dir}",
            space_parser="kb-total-free",  # "1,090,732 KB total (784,464 KB free)"
            extensions=(".cc", ".pat"),
            install=("startup system-software {path}",),
            reload=("reboot", "@~(?i)save~!", "@y"),
            version_cmd="display version",
            note="VRP takes a push only when its SCP/SFTP server is on ('scp server enable' / 'sftp server enable')",
        ),
        Vendor(
            "fortinet",
            "Fortinet FortiOS",
            transfer=TRANSFER_PULL,
            extensions=(".out",),
            # FortiOS has no command that hashes a file; it validates the image itself while installing it.
            version_cmd="get system status",
            # No flash-free figure exists on the CLI; free RAM is shown as a (non-blocking) sanity figure only.
            space_cmd="get system performance status",
            space_parser="fortios-memory",
            space_advisory=True,
            reload=("execute reboot", "@y"),
            note="FortiOS cannot receive firmware over SCP/SFTP: serve the file (TFTP) and run "
            "'execute restore image tftp FILE SERVER' on the device; it upgrades and restarts by itself. "
            "Confirm afterwards with 'verify --expect-version'.",
        ),
        Vendor(
            "aruba-aos-cx",
            "Aruba AOS-CX",
            transfer=TRANSFER_PULL,
            # Images live in primary/secondary slots, not files. 'show images' prints a SHA-256 per slot.
            verify_cmd="show images",
            verify_algo="sha256",
            verify_marker=r"Primary\s+Image",
            version_cmd="show version",
            extensions=(".swi",),
            install=("boot set-default primary",),
            reload=("boot system primary", "@y"),
            note="AOS-CX copies images into a slot ('copy tftp://SERVER/FILE primary', add 'vrf mgmt' if the server is "
            "reached through the management VRF); there is no file to push and no free-space figure for the slot.",
        ),
        Vendor(
            "paloalto-panos",
            "Palo Alto PAN-OS",
            transfer=TRANSFER_PULL,
            space_cmd="show system disk-space",
            space_parser="df-human",
            space_dir="/opt/panrepo",
            install=("request system software install version {version}", "@y"),
            install_label="install the software version found in the file name (asynchronous job)",
            version_cmd="show system info | match sw-version",
            # No reload: the install runs as a background job; restarting before it finishes would break the device.
            note="PAN-OS imports software itself ('scp import software from USER@SERVER:FILE'); it has no checksum "
            "command, so confirm with 'request system software info' / 'verify --expect-version'. The install is an "
            "asynchronous job: watch 'show jobs all', then 'request restart system' yourself.",
        ),
        Vendor(
            "vyos",
            "VyOS",
            verify_cmd="sha256sum {path}",
            verify_algo="sha256",
            extensions=(".iso",),
            # Op-mode commands: typed into an interactive session; each '@' accepts the installer's default answer.
            install=("add system image {path}", "@", "@", "@", "@"),
            install_label="install the file as a new system image (accepting the installer's defaults)",
            reload=("reboot", "@yes"),
            reload_in=("reboot in {minutes}", "@yes"),
            version_cmd="show version",
            version_interactive=True,
            **_DF,
        ),
        Vendor(
            "pfsense",
            "pfSense",
            verify_cmd="sha256 {path}",  # FreeBSD: "SHA256 (path) = <hex>"
            verify_algo="sha256",
            install=("pkg add {path}",),
            install_label="install the file as a FreeBSD package (pkg add)",
            reload=("shutdown -r now",),
            reload_in=("shutdown -r +{minutes}",),
            version_cmd="cat /etc/version",
            shell_hint_pattern=r"Enter an option|Welcome to pfSense",
            shell_hint="SSH opened the console menu, not a shell: log in as root, or set the user's shell to /bin/sh, "
            "so commands run directly",
            note="pfSense updates itself from its package repository (pfSense-upgrade); there is no local-image "
            "upgrade, so use push for packages and files.",
            **_DF,
        ),
        Vendor(
            "opnsense",
            "OPNsense",
            verify_cmd="sha256 {path}",
            verify_algo="sha256",
            install=("pkg add {path}",),
            install_label="install the file as a FreeBSD package (pkg add)",
            reload=("shutdown -r now",),
            reload_in=("shutdown -r +{minutes}",),
            version_cmd="opnsense-version",
            shell_hint_pattern=r"Enter an option|HTTPS|LAN \(",
            shell_hint="SSH opened the console menu, not a shell: log in as root, or choose option 8 (Shell) and "
            "enable shell access for the user",
            note="OPNsense updates itself with opnsense-update; there is no local-image upgrade, so use push for "
            "packages and files.",
            **_DF,
        ),
        Vendor(
            "ruijie-rgos",
            "Ruijie RGOS",
            transfer=TRANSFER_PULL,
            # RGOS checks the package MD5 itself during 'upgrade'; no file-hash command is documented.
            version_cmd="show version",
            space_cmd="dir",
            space_parser="auto",  # output format is not documented: only explicit "N KB/bytes ... free" figures count
            extensions=(".bin",),
            reload=("reload", "@y"),
            note="RGOS 12.x pulls the image with 'upgrade download tftp://SERVER/FILE' (effective after 'reload'); "
            "RGOS 10.x uses 'copy tftp: flash:' (destination name rgos.bin). Confirm with 'verify --expect-version'.",
        ),
        Vendor(
            "sophos-sfos",
            "Sophos SFOS",
            transfer=TRANSFER_WEB,
            verify_cmd="sha256sum {path}",
            verify_algo="sha256",
            version_cmd=None,
            shell_hint_pattern=r"Select Menu Number|Main Menu|Device Management",
            shell_hint="SFOS SSH opens its console menu: choose Device Management, then Advanced Shell, to get a "
            "shell; commands cannot be run directly over SSH",
            note="SFOS firmware is installed through the web admin (Backup & firmware > Upload Firmware / Upload & "
            "Boot); no CLI install command is documented. Hash checks only work from the Advanced Shell.",
            **_DF,
        ),
        Vendor(
            "checkpoint-gaia",
            "Check Point Gaia",
            verify_cmd="sha256sum {path}",
            verify_algo="sha256",
            extensions=(".tgz",),
            # Commands below run in Expert (bash). Gaia Clish commands go through 'clish -c "..."'.
            install=('clish -c "installer import local {path}"',),
            install_label="import the package into CPUSE (then run 'installer verify' / 'installer install' in clish)",
            reload=('clish -c "reboot"',),
            reload_in=("shutdown -r +{minutes}",),
            version_cmd='clish -c "show version all"',
            shell_hint_pattern=r"CLINFR\d+|Invalid command",
            shell_hint=_CLISH_HINT,
            note="SCP and SSH commands need the login shell to be bash (Expert), not Gaia Clish.",
            **_DF,
        ),
        Vendor(
            "linux",
            "Linux / generic (POSIX shell)",
            verify_cmd="sha256sum {path}",
            verify_algo="sha256",
            **_DF,
        ),
    )
}

ALIASES = {
    "cisco": "cisco-ios",
    "ios": "cisco-ios",
    "iosxe": "cisco-ios",
    "nxos": "cisco-nxos",
    "arista": "arista-eos",
    "eos": "arista-eos",
    "juniper": "junos",
    "junipper": "junos",
    "huawei-vrp": "huawei",
    "vrp": "huawei",
    "fortigate": "fortinet",
    "fortios": "fortinet",
    "aruba": "aruba-aos-cx",
    "aos-cx": "aruba-aos-cx",
    "aoscx": "aruba-aos-cx",
    "paloalto": "paloalto-panos",
    "palo-alto": "paloalto-panos",
    "panos": "paloalto-panos",
    "pan-os": "paloalto-panos",
    "ruijie": "ruijie-rgos",
    "rgos": "ruijie-rgos",
    "sophos": "sophos-sfos",
    "sfos": "sophos-sfos",
    "checkpoint": "checkpoint-gaia",
    "check-point": "checkpoint-gaia",
    "gaia": "checkpoint-gaia",
}


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


_VERSION = re.compile(r"(\d+\.\d+\.\d+(?:-h\d+)?)")


def version_from_filename(name: str) -> Optional[str]:
    """Software version embedded in an image file name, e.g. ``PanOS_220-10.1.11-h1`` -> ``10.1.11-h1``."""
    found = _VERSION.findall(name)
    return found[-1] if found else None
