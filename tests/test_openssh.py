"""Integration against a real OpenSSH sshd (SCP, SFTP, key auth, legacy-only algorithms). Linux only.

Skipped when sshd/ssh-keygen are not installed. Uses key auth only, so it also runs as a normal user.
"""

import getpass
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from app.core.events import State
from app.core.known_hosts import KnownHostsStore
from app.core.scp_service import ScpRequest, ScpService

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
KEYGEN = shutil.which("ssh-keygen")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not (SSHD and KEYGEN),
    reason="needs a Linux OpenSSH sshd + ssh-keygen (other OS runners ship differently configured sshd builds)",
)

BASE = """Port {port}
ListenAddress 127.0.0.1
HostKey {hostkey}
PidFile {tmp}/sshd.pid
AuthorizedKeysFile {authkeys}
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
StrictModes no
PermitRootLogin yes
Subsystem sftp internal-sftp
LogLevel ERROR
"""
LEGACY = """KexAlgorithms diffie-hellman-group14-sha1
HostKeyAlgorithms ssh-rsa
Ciphers aes128-cbc
MACs hmac-sha1
PubkeyAcceptedAlgorithms ssh-ed25519
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _keygen(path, kind, *extra):
    subprocess.run([KEYGEN, "-q", "-t", kind, "-N", "", "-f", str(path), *extra], check=True)


@pytest.fixture(scope="module")
def client_key(tmp_path_factory):
    d = tmp_path_factory.mktemp("clientkey")
    _keygen(d / "id", "ed25519", "-m", "PEM") if False else _keygen(d / "id", "ed25519")
    return d / "id", (d / "id.pub").read_text()


def start_sshd(tmp, hostkey_kind, extra, client_pub):
    hostkey = tmp / f"host_{hostkey_kind}"
    _keygen(hostkey, hostkey_kind)
    authkeys = tmp / "authorized_keys"
    authkeys.write_text(client_pub)
    port = _free_port()
    cfg = tmp / "sshd_config"
    cfg.write_text(BASE.format(port=port, hostkey=hostkey, tmp=tmp, authkeys=authkeys) + extra)
    if os.name == "posix" and os.geteuid() == 0:
        os.makedirs("/run/sshd", exist_ok=True)  # privilege-separation directory
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(cfg)], stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 6
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.skip(f"sshd would not start with this config: {proc.stderr.read()[:200]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    pytest.skip("sshd did not open its port in time")


@pytest.fixture
def sshd(tmp_path, client_key):
    proc, port = start_sshd(tmp_path, "ed25519", "", client_key[1])
    yield port
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def sshd_legacy(tmp_path, client_key):
    proc, port = start_sshd(tmp_path, "rsa", LEGACY, client_key[1])
    yield port
    proc.terminate()
    proc.wait(5)


def push(port, key, src, remote, protocol, tmp_path):
    events = []
    ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(
        ScpRequest(
            "127.0.0.1",
            getpass.getuser(),
            str(src),
            port=port,
            remote_path=str(remote),
            protocol=protocol,
            key_path=str(key),
        )
    )
    return events


@pytest.mark.parametrize("protocol", ["scp", "sftp"])
def test_push_into_directory(sshd, client_key, tmp_path, protocol):
    dest = tmp_path / "dest"
    dest.mkdir()
    src = tmp_path / "image.bin"
    src.write_bytes(os.urandom(400_000))
    events = push(sshd, client_key[0], src, str(dest) + ("/" if protocol == "sftp" else ""), protocol, tmp_path)
    assert events[-1].state is State.FINISHED, events[-1]
    assert (dest / "image.bin").read_bytes() == src.read_bytes()


def test_sftp_directory_without_trailing_slash_and_full_file_path(sshd, client_key, tmp_path):
    dest = tmp_path / "d2"
    dest.mkdir()
    src = tmp_path / "a.bin"
    src.write_bytes(b"A" * 1000)
    assert push(sshd, client_key[0], src, dest, "sftp", tmp_path)[-1].state is State.FINISHED
    assert (dest / "a.bin").exists()
    assert push(sshd, client_key[0], src, dest / "renamed.bin", "sftp", tmp_path)[-1].state is State.FINISHED
    assert (dest / "renamed.bin").read_bytes() == b"A" * 1000


def test_legacy_only_server_still_connects(sshd_legacy, client_key, tmp_path):
    """Like old Cisco/Huawei gear: SHA-1 key exchange, ssh-rsa host key, CBC cipher, hmac-sha1."""
    dest = tmp_path / "ldest"
    dest.mkdir()
    src = tmp_path / "old.bin"
    src.write_bytes(os.urandom(50_000))
    events = push(sshd_legacy, client_key[0], src, str(dest), "scp", tmp_path)
    assert events[-1].state is State.FINISHED, events[-1]
    assert (dest / "old.bin").read_bytes() == src.read_bytes()
