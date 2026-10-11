import os
import socket
import threading
import time

import pytest
import tftpy

from app.core.events import Level, LogEvent, ProgressEvent, State, StateEvent
from app.core.tftp_service import TftpService


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path):
    payload = os.urandom(200_000)
    src = tmp_path / "firmware.bin"
    src.write_bytes(payload)
    events, port = [], _free_udp_port()
    svc = TftpService(events.append, select_timeout=0.5)
    t = threading.Thread(target=svc.serve, args=(str(src), "127.0.0.1", port), daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any(isinstance(e, StateEvent) and e.state is State.STARTED for e in events):
        time.sleep(0.02)
    yield payload, port, events, svc, t
    svc.stop()
    t.join(5)


def test_serves_selected_file_under_any_name(served, tmp_path):
    payload, port, events, _svc, _t = served
    out = tmp_path / "got.bin"
    tftpy.TftpClient("127.0.0.1", port).download("whatever-name.img", str(out))
    assert out.read_bytes() == payload
    time.sleep(0.2)
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    assert progress and progress[-1].sent == progress[-1].total == len(payload)
    assert any(isinstance(e, LogEvent) and e.level is Level.SUCCESS and "sent" in e.message for e in events)
    assert any(isinstance(e, LogEvent) and "whatever-name.img" in e.message for e in events)


def test_uploads_are_rejected(served, tmp_path):
    _, port, events, _, _ = served
    junk = tmp_path / "junk.txt"
    junk.write_bytes(b"x" * 10)
    with pytest.raises(tftpy.TftpException):
        tftpy.TftpClient("127.0.0.1", port).upload("junk.txt", str(junk))
    time.sleep(0.2)
    assert any(isinstance(e, LogEvent) and "rejected" in e.message for e in events)


def test_stop_emits_stopped(served):
    _, _, events, svc, t = served
    svc.stop()
    t.join(5)
    assert not t.is_alive()
    assert events[-1] == StateEvent(State.STOPPED)


def test_missing_file_fails_cleanly(tmp_path):
    events = []
    TftpService(events.append).serve(str(tmp_path / "nope.bin"), "127.0.0.1", 6969)
    assert events[-1].state is State.FAILED


def test_port_in_use_reports_failure(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"abc")
    events = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        TftpService(events.append).serve(str(src), "127.0.0.1", blocker.getsockname()[1])
    assert events[-1].state is State.FAILED and "already in use" in events[-1].detail
    assert not any(isinstance(e, StateEvent) and e.state is State.STARTED for e in events)
