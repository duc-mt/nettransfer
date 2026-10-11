"""Host-key TOFU, key auth, keyboard-interactive, connection tester (in-process SSH stub)."""

import os
import socket

import paramiko
import pytest

from app.core.events import Level, LogEvent, State, StateEvent
from app.core.known_hosts import CHANGED, MATCH, NEW, KnownHostsStore
from app.core.precheck import ConnectionTester
from app.core.scp_service import ScpRequest, ScpService


def _src(tmp_path, size=50_000):
    f = tmp_path / "f.bin"
    f.write_bytes(os.urandom(size))
    return f


def test_new_key_is_trusted_and_persisted(ssh_stub, tmp_path):
    port, _state = ssh_stub
    store_path = tmp_path / "kh" / "known_hosts"
    events = []
    f = _src(tmp_path)
    ScpService(events.append, KnownHostsStore(store_path)).push(
        ScpRequest("127.0.0.1", "admin", str(f), password="secret", port=port)
    )
    assert events[-1].state is State.FINISHED
    assert any(isinstance(e, LogEvent) and "trusted on first use" in e.message for e in events)
    assert f"[127.0.0.1]:{port}" in store_path.read_text()
    if os.name == "posix":
        assert oct(store_path.stat().st_mode & 0o777) == "0o600"


def test_changed_host_key_is_refused_unless_accepted(ssh_stub, tmp_path):
    port, _ = ssh_stub
    store = KnownHostsStore(tmp_path / "known_hosts")
    store.trust("127.0.0.1", port, paramiko.RSAKey.generate(2048))  # a different key is already trusted
    events = []
    ScpService(events.append, store).push(
        ScpRequest("127.0.0.1", "admin", str(_src(tmp_path)), password="secret", port=port)
    )
    assert events[-1].state is State.FAILED and "CHANGED" in events[-1].detail
    assert not any(isinstance(e, StateEvent) and e.state is State.STARTED for e in events)  # nothing sent


def test_changed_host_key_can_be_replaced(ssh_stub, tmp_path):
    port, state = ssh_stub
    store = KnownHostsStore(tmp_path / "known_hosts")
    store.trust("127.0.0.1", port, paramiko.RSAKey.generate(2048))
    events = []
    f = _src(tmp_path)
    ScpService(events.append, store).push(
        ScpRequest("127.0.0.1", "admin", str(f), password="secret", port=port, replace_host_key=True)
    )
    assert events[-1].state is State.FINISHED and state["received"] == f.read_bytes()
    assert any(isinstance(e, LogEvent) and e.level is Level.WARNING and "replaced" in e.message for e in events)


def test_known_hosts_store_states(tmp_path):
    store = KnownHostsStore(tmp_path / "kh")
    a, b = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
    assert store.check("h", 22, a) == NEW
    store.trust("h", 22, a)
    assert store.check("h", 22, a) == MATCH and store.check("h", 22, b) == CHANGED
    assert store.check("h", 2222, a) == NEW  # port is part of the identity
    store.trust("h", 22, b)  # replace
    assert store.check("h", 22, b) == MATCH
    assert KnownHostsStore(None).check("h", 22, a) == NEW  # in-memory store works too


@pytest.mark.parametrize("passphrase", ["", "s3cret-pass"])
def test_public_key_auth(ssh_stub, tmp_path, passphrase):
    port, state = ssh_stub
    key = paramiko.RSAKey.generate(2048)
    key_file = tmp_path / "id_rsa"
    key.write_private_key_file(str(key_file), password=passphrase or None)
    state["server"].client_key = key
    events = []
    f = _src(tmp_path)
    ScpService(events.append).push(
        ScpRequest("127.0.0.1", "admin", str(f), port=port, key_path=str(key_file), key_passphrase=passphrase)
    )
    assert events[-1].state is State.FINISHED and state["received"] == f.read_bytes()
    assert not any("s3cret-pass" in repr(e) for e in events)


def test_encrypted_key_without_passphrase_explains(ssh_stub, tmp_path):
    port, _ = ssh_stub
    key_file = tmp_path / "id_rsa"
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_file), password="pw")
    events = []
    ScpService(events.append).push(
        ScpRequest("127.0.0.1", "admin", str(_src(tmp_path)), port=port, key_path=str(key_file))
    )
    assert events[-1].state is State.FAILED and "passphrase" in events[-1].detail


def test_keyboard_interactive_fallback(ssh_stub, tmp_path):
    port, state = ssh_stub
    state["server"].kbd_only = True
    events = []
    f = _src(tmp_path)
    ScpService(events.append).push(ScpRequest("127.0.0.1", "admin", str(f), password="secret", port=port))
    assert events[-1].state is State.FINISHED and state["received"] == f.read_bytes()


def test_connection_tester_ssh(ssh_stub):
    port, _ = ssh_stub
    events = []
    ConnectionTester(events.append).run("127.0.0.1", port, "scp")
    text = " | ".join(e.message for e in events if isinstance(e, LogEvent))
    assert "TCP" in text and "paramiko" in text and "not seen before" in text
    assert events[-1] == StateEvent(State.FINISHED, "127.0.0.1")


def test_connection_tester_reports_closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    events = []
    ConnectionTester(events.append).run("127.0.0.1", port, "sftp", timeout=2)
    assert events[-1].state is State.FAILED
    assert any(msg in events[-1].detail.lower() for msg in ("refused", "timed out"))


def test_wrong_key_passphrase_and_unneeded_passphrase(ssh_stub, tmp_path):
    port, _state = ssh_stub
    key = paramiko.RSAKey.generate(2048)
    enc = tmp_path / "enc"
    key.write_private_key_file(str(enc), password="right")
    events = []
    ScpService(events.append).push(
        ScpRequest("127.0.0.1", "admin", str(_src(tmp_path)), port=port, key_path=str(enc), key_passphrase="wrong")
    )
    assert events[-1].state is State.FAILED and "wrong passphrase" in events[-1].detail


def test_passphrase_given_for_unencrypted_key_is_tolerated(ssh_stub, tmp_path):
    port, state = ssh_stub
    key = paramiko.RSAKey.generate(2048)
    plain = tmp_path / "plain"
    key.write_private_key_file(str(plain))
    state["server"].client_key = key
    events = []
    ScpService(events.append).push(
        ScpRequest("127.0.0.1", "admin", str(_src(tmp_path)), port=port, key_path=str(plain), key_passphrase="unneeded")
    )
    assert events[-1].state is State.FINISHED
