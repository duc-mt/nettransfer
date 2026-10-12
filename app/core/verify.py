"""Post-transfer verification: ask the device for the digest of the file it received and compare with the local one."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import paramiko

from .hashing import file_digests
from .remote import RemoteError, run_exec
from .vendors import Vendor, get_vendor, is_safe_device_path

_DIGEST_LENGTH = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}


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


def parse_digest(output: str, algo: str) -> Optional[str]:
    """The *last* hex string of the right length in the output (devices echo the path first, the digest last)."""
    length = _DIGEST_LENGTH[algo]
    found = re.findall(rf"(?<![0-9a-fA-F])[0-9a-fA-F]{{{length}}}(?![0-9a-fA-F])", output)
    return found[-1].lower() if found else None


def verify_remote(
    transport: paramiko.Transport,
    local_path: str,
    remote_path: str,
    name: str,
    opts: VerifyOptions,
    vendor: Optional[Vendor] = None,
) -> VerifyResult:
    template = opts.command or (vendor.verify_cmd if vendor else None)
    if not template:
        label = vendor.label if vendor else "this device"
        return VerifyResult(False, f"no verify command known for {label}: pass --verify-cmd")
    algo = (opts.algo or (vendor.verify_algo if vendor and not opts.command else "md5")).lower()
    if algo not in _DIGEST_LENGTH:
        return VerifyResult(False, f"unsupported digest '{algo}' (use {', '.join(_DIGEST_LENGTH)})")
    if not (is_safe_device_path(remote_path) and is_safe_device_path(name)):
        return VerifyResult(
            False,
            f"refusing to build a device command from '{remote_path}': only letters, digits and . _ / : @ % + = , ~ -"
            " are allowed (rename the file or skip --verify)",
        )
    command = template.format(path=remote_path, file=name)
    local = file_digests(local_path, (algo,))[algo]
    try:
        output, _status = run_exec(transport, command, opts.timeout)
    except RemoteError as exc:
        return VerifyResult(False, str(exc), algo, local)
    remote = parse_digest(output, algo)
    if remote is None:
        snippet = " ".join(output.split())[:160] or "(no output)"
        return VerifyResult(False, f"could not read a {algo.upper()} from the device reply: {snippet}", algo, local)
    if remote != local:
        return VerifyResult(
            False,
            f"{algo.upper()} MISMATCH: device {remote} != local {local} (the file is corrupt)",
            algo,
            local,
            remote,
        )
    return VerifyResult(True, f"{algo.upper()} {local} matches on the device", algo, local, remote)


def resolve_vendor(vendor_id: str) -> Optional[Vendor]:
    return get_vendor(vendor_id) if vendor_id else None
