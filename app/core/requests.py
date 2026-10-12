"""Build per-protocol push requests from shared defaults (+ optional per-device overrides).

Used by both the GUI and the CLI so the two never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from .batch import BatchTarget
from .events import EventSink
from .ftp_service import FtpRequest, FtpService
from .install import InstallOptions
from .known_hosts import KnownHostsStore
from .scp_service import ScpRequest, ScpService
from .ssh_session import JumpHost
from .verify import VerifyOptions

PROTOCOLS = ("scp", "sftp", "ftp", "ftps")
Request = Union[ScpRequest, FtpRequest]


def is_ftp(protocol: str) -> bool:
    return protocol in ("ftp", "ftps")


def default_port(protocol: str) -> int:
    return 21 if is_ftp(protocol) else 22


@dataclass(frozen=True)
class PushDefaults:
    protocol: str = "scp"
    username: str = ""
    password: str = field(default="", repr=False)
    key_path: str = ""
    key_passphrase: str = field(default="", repr=False)
    port: Optional[int] = None  # None -> protocol default
    remote_path: str = ""
    replace_host_key: bool = False
    vendor: str = ""
    preflight: bool = False
    verify: Optional[VerifyOptions] = None
    install: Optional[InstallOptions] = None
    resume: bool = False
    jump: Optional[JumpHost] = None
    # (host, username) -> password from the OS keyring; consulted only when no password was given for the device
    credentials: Optional[Callable[[str, str], Optional[str]]] = field(default=None, repr=False)

    def build(self, host: str, local_path: str, target: Optional[BatchTarget] = None) -> Request:
        user = target.username if target and target.username else self.username
        password = target.password if target and target.password else self.password
        if not password and self.credentials is not None and user:
            password = self.credentials(host, user) or ""
        remote = target.remote_path if target and target.remote_path else self.remote_path
        port = (target.port if target and target.port else self.port) or default_port(self.protocol)
        if is_ftp(self.protocol):
            return FtpRequest(
                host, user, local_path, password, port, remote, tls=self.protocol == "ftps", resume=self.resume
            )
        return ScpRequest(
            host,
            user,
            local_path,
            password,
            port,
            remote,
            protocol=self.protocol,
            key_path=self.key_path,
            key_passphrase=self.key_passphrase,
            replace_host_key=self.replace_host_key,
            jump=self.jump,
            vendor=(target.vendor if target and target.vendor else self.vendor),
            preflight=self.preflight,
            verify=self.verify,
            install=self.install,
            resume=self.resume,
        )


def make_service_factory(protocol: str, known_hosts: KnownHostsStore) -> Callable[[EventSink], object]:
    if is_ftp(protocol):
        return FtpService
    return lambda sink: ScpService(sink, known_hosts)
