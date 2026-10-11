import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from app.core.batch import BatchFileError, BatchRunner, BatchTarget, load_targets_csv, write_results_csv
from app.core.events import BatchEvent, LogEvent, ProgressEvent, State, StateEvent
from app.core.ftp_service import FtpRequest, FtpService
from app.core.precheck import ConnectionTester


# ---------------------------------------------------------------- FTP (pyftpdlib)
@pytest.fixture
def ftpd(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "ftproot"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user("dev", "pw", str(root), perm="elradfmwMT")

    class Handler(FTPHandler):
        authorizer = auth
        banner = "NetTransfer test FTP ready"

    server = FTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(timeout=0.2, handle_exit=False), daemon=True)
    thread.start()
    yield server.address[1], root
    server.close_all()
    thread.join(5)


def test_ftp_push_success(ftpd, tmp_path):
    port, root = ftpd
    payload = os.urandom(200_000)
    src = tmp_path / "image.bin"
    src.write_bytes(payload)
    events = []
    FtpService(events.append).push(FtpRequest("127.0.0.1", "dev", str(src), password="pw", port=port))
    assert events[-1] == StateEvent(State.FINISHED, "127.0.0.1")
    assert (root / "image.bin").read_bytes() == payload
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    assert progress[-1].sent == progress[-1].total == len(payload)
    assert not any("pw" == getattr(e, "message", "") for e in events)


def test_ftp_remote_path_variants(ftpd, tmp_path):
    port, root = ftpd
    (root / "sub").mkdir()
    src = tmp_path / "a.bin"
    src.write_bytes(b"abc")
    for remote, expected in (("sub/", "sub/a.bin"), ("renamed.bin", "renamed.bin")):
        events = []
        FtpService(events.append).push(
            FtpRequest("127.0.0.1", "dev", str(src), password="pw", port=port, remote_path=remote)
        )
        assert events[-1].state is State.FINISHED, events[-1]
        assert (root / expected).read_bytes() == b"abc"


def test_ftp_bad_login_and_refused(ftpd, tmp_path):
    port, _ = ftpd
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    events = []
    FtpService(events.append).push(FtpRequest("127.0.0.1", "dev", str(src), password="WRONG", port=port))
    assert events[-1].state is State.FAILED and "login failed" in events[-1].detail
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    events = []
    FtpService(events.append).push(FtpRequest("127.0.0.1", "dev", str(src), password="pw", port=closed))
    assert events[-1].state is State.FAILED and "refused" in events[-1].detail.lower()


def test_ftp_connection_tester(ftpd):
    port, _ = ftpd
    events = []
    ConnectionTester(events.append).run("127.0.0.1", port, "ftp")
    assert any(isinstance(e, LogEvent) and "NetTransfer test FTP" in e.message for e in events)
    assert events[-1].state is State.FINISHED


# ---------------------------------------------------------------- batch CSV
def write_csv(tmp_path, text):
    p = tmp_path / "t.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_csv_loads_defaults_and_overrides(tmp_path):
    path = write_csv(
        tmp_path,
        "# firmware rollout\nHost, Username ,remote_path,port\n10.0.0.1,,flash:,\n10.0.0.2,ops,/var/tmp/,2222\n\n",
    )
    t = load_targets_csv(path)
    assert [x.host for x in t] == ["10.0.0.1", "10.0.0.2"]
    assert t[0].username == "" and t[0].port is None and t[0].remote_path == "flash:"
    assert t[1].username == "ops" and t[1].port == 2222 and t[1].index == 1


@pytest.mark.parametrize(
    "text,needle",
    [
        ("username\nops\n", "host"),
        ("host,color\n1.1.1.1,red\n", "unknown column"),
        ("host,port\n1.1.1.1,99999\n,22\n", "line 2"),
        ("host\n", "no device rows"),
        ("", "empty"),
    ],
)
def test_csv_errors_abort_the_load(tmp_path, text, needle):
    with pytest.raises(BatchFileError) as exc:
        load_targets_csv(write_csv(tmp_path, text))
    assert needle in str(exc.value)


# ---------------------------------------------------------------- batch runner
class FakeService:
    live, peak, lock = 0, 0, threading.Lock()

    def __init__(self, sink):
        self.sink, self.cancelled = sink, threading.Event()

    def push(self, req):
        cls = FakeService
        with cls.lock:
            cls.live += 1
            cls.peak = max(cls.peak, cls.live)
        try:
            if req.host == "boom":
                raise RuntimeError("kaboom")
            self.sink(StateEvent(State.STARTED, req.host))
            if req.host == "slow":
                self.sink(ProgressEvent(1, 10, 1.0, req.host))
                if self.cancelled.wait(5):
                    self.sink(StateEvent(State.STOPPED, "cancelled"))
                    return
            time.sleep(0.05)
            self.sink(LogEvent(__import__("app.core.events", fromlist=["Level"]).Level.INFO, "hello"))
            if req.host.startswith("bad"):
                self.sink(StateEvent(State.FAILED, "Authentication failed"))
            else:
                self.sink(ProgressEvent(10, 10, 5.0, req.host))
                self.sink(StateEvent(State.FINISHED, req.host))
        finally:
            with cls.lock:
                cls.live -= 1

    def cancel(self):
        self.cancelled.set()


def make_runner(events, workers=2):
    FakeService.live = FakeService.peak = 0
    return BatchRunner(events.append, FakeService, lambda t: SimpleNamespace(host=t.host), max_workers=workers)


def targets(*hosts):
    return [BatchTarget(i, h) for i, h in enumerate(hosts)]


def test_batch_results_and_events():
    events = []
    runner = make_runner(events, workers=3)
    runner.run(targets("a", "bad1", "b", "boom"))
    assert runner.results[0] == ("done", "") and runner.results[2] == ("done", "")
    assert runner.results[1] == ("failed", "Authentication failed")
    assert runner.results[3][0] == "failed" and "kaboom" in runner.results[3][1]
    queued = [e for e in events if isinstance(e, BatchEvent) and e.status == "queued"]
    assert len(queued) == 4
    assert any(isinstance(e, LogEvent) and e.message == "[a] hello" for e in events)  # host-prefixed log
    assert events[-1].state is State.FINISHED and "2 ok, 2 failed" in events[-1].detail


def test_batch_respects_concurrency_limit():
    runner = make_runner([], workers=2)
    runner.run(targets(*[f"h{i}" for i in range(8)]))
    assert 1 <= FakeService.peak <= 2
    assert all(s == "done" for s, _ in runner.results.values())


def test_batch_cancel_marks_running_and_queued():
    events = []
    runner = make_runner(events, workers=1)
    thread = threading.Thread(target=runner.run, args=(targets("slow", "x", "y"),))
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any(isinstance(e, BatchEvent) and e.status == "running" for e in events):
        time.sleep(0.02)
    runner.cancel()
    thread.join(10)
    assert not thread.is_alive()
    assert [runner.results[i][0] for i in range(3)] == ["cancelled", "cancelled", "cancelled"]


def test_results_csv(tmp_path):
    ts = targets("a", "b")
    out = tmp_path / "r.csv"
    write_results_csv(str(out), ts, {0: ("done", ""), 1: ("failed", "no route, sorry")})
    lines = out.read_text().splitlines()
    assert lines[0] == "host,status,detail" and lines[1] == "a,done," and '"no route, sorry"' in lines[2]
