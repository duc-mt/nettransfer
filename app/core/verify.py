"""Post-transfer verification: ask the device for the digest of the file it received and compare with the local one.

Platforms with no file-hash command (FortiOS, PAN-OS, Ruijie, ...) can be checked by *version* instead
(``check_version``): that proves which software is running, not that a file is intact, and is reported as such.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional

import paramiko

from .hashing import file_digests
from .remote import RemoteError, Shell, run_exec
from .vendors import Vendor, get_vendor, is_safe_device_path

_DIGEST_LENGTH = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}

STATUS_VERIFIED = "VERIFIED (hash match)"
STATUS_MISMATCH = "FAILED (checksum mismatch)"
STATUS_ERROR = "FAILED (verification error)"
STATUS_VERSION_OK = "VERIFIED (version match)"
STATUS_VERSION_BAD = "FAILED (version mismatch)"


@dataclass(frozen=True)
class VerifyOptions:
    command: str = ""  # custom template, placeholders {path} {file}; overrides the vendor profile
    algo: str = ""  # md5 | sha1 | sha256 | sha512; blank = the profile's (md5 for custom commands)
    timeout: float = 900.0  # large images take minutes to hash on a small CPU


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    detail: str
    algo: str = ""
    local: str = ""
    remote: str = ""
    mismatch: bool = False  # the device answered with a digest/version and it differs
    kind: str = "hash"  # "hash" | "version"

    @property
    def status(self) -> str:
        if self.kind == "version":
            return STATUS_VERSION_OK if self.ok else STATUS_VERSION_BAD if self.mismatch else STATUS_ERROR
        return STATUS_VERIFIED if self.ok else STATUS_MISMATCH if self.mismatch else STATUS_ERROR


def check_verify_template(template: str) -> Optional[str]:
    """Why a custom verify command is unusable (None = fine). It may only use {path} and {file}, and should use one."""
    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as exc:
        return f"the verify command is malformed: {exc}"
    unknown = sorted(f or "(empty)" for f in fields - {"path", "file"})
    if unknown:
        return f"the verify command may only use {{path}} and {{file}}, not {{{unknown[0]}}}"
    if not fields:
        return "the verify command must contain {path} (the file on the device) or {file}"
    return None


def parse_digest(output: str, algo: str, marker: str = "") -> Optional[str]:
    """The *last* hex string of the right length in the output (devices echo the path first, the digest last). With a
    ``marker`` regex (a reply that lists several images) the *first* digest after that text is used instead."""
    length = _DIGEST_LENGTH[algo]
    pattern = rf"(?<![0-9a-fA-F])[0-9a-fA-F]{{{length}}}(?![0-9a-fA-F])"
    if marker:
        start = re.search(marker, output, re.IGNORECASE)
        if start is None:
            return None
        found = re.findall(pattern, output[start.end() :])
        return found[0].lower() if found else None
    found = re.findall(pattern, output)
    return found[-1].lower() if found else None


def _with_hint(detail: str, output: str, vendor: Optional[Vendor]) -> str:
    if vendor is not None and vendor.shell_hint_pattern and re.search(vendor.shell_hint_pattern, output):
        return f"{detail} - {vendor.shell_hint}"
    return detail


def evaluate_output(
    output: str, local: str, algo: str, marker: str = "", vendor: Optional[Vendor] = None
) -> VerifyResult:
    """Compare a device's reply with the locally computed digest (pure: no I/O, easy to test)."""
    label = algo.upper()
    remote = parse_digest(output, algo, marker)
    if remote is None:
        snippet = " ".join(output.split())[:160] or "(no output)"
        detail = f"could not read a {label} from the device reply: {snippet}"
        return VerifyResult(False, _with_hint(detail, output, vendor), algo, local)
    if remote != local.lower():
        return VerifyResult(
            False,
            f"{label} device {remote} != local {local.lower()}; the file on the device is corrupt",
            algo,
            local.lower(),
            remote,
            mismatch=True,
        )
    return VerifyResult(True, f"{label} {remote}", algo, local.lower(), remote)


def _run_interactive(transport: paramiko.Transport, lines: list, timeout: float) -> str:
    shell = Shell(transport, timeout=timeout)
    try:
        return shell.run_script(lines)
    finally:
        shell.close()


def verify_remote(
    transport: paramiko.Transport,
    local_path: str,
    remote_path: str,
    name: str,
    opts: VerifyOptions,
    vendor: Optional[Vendor] = None,
) -> VerifyResult:
    use_profile = vendor is not None and not opts.command
    steps = vendor.verify_steps if use_profile and vendor is not None else ()
    template = opts.command or (vendor.verify_cmd if vendor else None)
    if not template and not steps:
        label = vendor.label if vendor else "this device"
        return VerifyResult(
            False, f"no built-in verify command for {label}: set a custom verify command (--verify-cmd)"
        )
    if template:
        bad_template = check_verify_template(template)
        if bad_template and opts.command:
            return VerifyResult(False, bad_template)
    algo = (opts.algo or (vendor.verify_algo if use_profile and vendor else "md5")).lower()
    if algo not in _DIGEST_LENGTH:
        return VerifyResult(False, f"unsupported digest '{algo}' (use {', '.join(_DIGEST_LENGTH)})")
    needs_path = "{path}" in (template or "") or any("{path}" in s for s in steps)
    if needs_path and not (is_safe_device_path(remote_path) and is_safe_device_path(name)):
        return VerifyResult(
            False,
            f"refusing to build a device command from '{remote_path}': only letters, digits and . _ / : @ % + = , ~ -"
            " are allowed (rename the file or skip --verify)",
        )
    local = file_digests(local_path, (algo,))[algo]
    marker = vendor.verify_marker if use_profile and vendor else ""
    try:
        if steps:
            output = _run_interactive(transport, [s.format(path=remote_path, file=name) for s in steps], opts.timeout)
        else:
            output, _status = run_exec(transport, (template or "").format(path=remote_path, file=name), opts.timeout)
    except RemoteError as exc:
        return VerifyResult(False, str(exc), algo, local)
    return evaluate_output(output, local, algo, marker, vendor)


def check_version(transport: paramiko.Transport, vendor: Vendor, expected: str, timeout: float = 60.0) -> VerifyResult:
    """Confirm the device runs the expected software version (a text match on the vendor's version command)."""
    if not vendor.version_cmd:
        return VerifyResult(False, f"no version command known for {vendor.label}", kind="version")
    if not expected.strip():
        return VerifyResult(False, "give the version to look for (e.g. --expect-version 7.2.4)", kind="version")
    try:
        if vendor.version_interactive:
            output = _run_interactive(transport, [vendor.version_cmd], timeout)
        else:
            output, _ = run_exec(transport, vendor.version_cmd, timeout)
    except RemoteError as exc:
        return VerifyResult(False, str(exc), kind="version")
    if not output.strip():
        return VerifyResult(False, _with_hint("the device returned no output", output, vendor), kind="version")
    if expected.lower() in output.lower():
        return VerifyResult(
            True, f"'{expected}' found in the reply of '{vendor.version_cmd}'", remote=expected, kind="version"
        )
    first = " ".join(output.split())[:120]
    return VerifyResult(
        False,
        _with_hint(f"'{expected}' not found in the reply of '{vendor.version_cmd}': {first}", output, vendor),
        remote=first,
        mismatch=True,
        kind="version",
    )


def resolve_vendor(vendor_id: str) -> Optional[Vendor]:
    return get_vendor(vendor_id) if vendor_id else None
