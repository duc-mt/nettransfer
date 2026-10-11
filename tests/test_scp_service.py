"""SCP service against the in-process SSH/SCP stub from conftest.py."""

import os
import socket

from app.core.events import Level, LogEvent, ProgressEvent, State, StateEvent
from app.core.scp_service import ScpRequest, ScpService


def test_push_success(ssh_stub, tmp_path):
    port, state = ssh_stub
    payload = os.urandom(300_000)
    src = tmp_path / "image.bin"
    src.write_bytes(payload)
    events = []
    req = ScpRequest("127.0.0.1", "admin", str(src), password="secret", port=port, remote_path="flash:")
    ScpService(events.append).push(req)
    assert events[-1] == StateEvent(State.FINISHED, "127.0.0.1")
    assert state["received"] == payload and state["name"] == "image.bin"
    assert "flash:" in state["server"].command
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    assert progress[-1].sent == progress[-1].total == len(payload)
    assert any(isinstance(e, LogEvent) and e.level is Level.SUCCESS for e in events)
    assert not any("secret" in repr(e) for e in events)


def test_wrong_password_reports_auth_failure(ssh_stub, tmp_path):
    port, _ = ssh_stub
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    events = []
    ScpService(events.append).push(ScpRequest("127.0.0.1", "admin", str(src), password="nope", port=port))
    assert events[-1].state is State.FAILED and "Authentication failed" in events[-1].detail


def test_connection_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    events = []
    import tempfile

    with tempfile.NamedTemporaryFile() as f:
        ScpService(events.append).push(ScpRequest("127.0.0.1", "a", f.name, password="b", port=port))
    assert events[-1].state is State.FAILED and "refused" in events[-1].detail.lower()


def test_missing_file(tmp_path):
    events = []
    ScpService(events.append).push(ScpRequest("127.0.0.1", "a", str(tmp_path / "x"), password="b"))
    assert events[-1].state is State.FAILED


def test_request_repr_hides_password(tmp_path):
    assert "secret" not in repr(ScpRequest("h", "u", "f", password="secret"))
