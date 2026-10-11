import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import tftpy

from app import cli
from app.core.known_hosts import KnownHostsStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_store", lambda: KnownHostsStore(tmp_path / "known_hosts"))


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def src(tmp_path, size=60_000):
    f = tmp_path / "fw.bin"
    f.write_bytes(os.urandom(size))
    return f


def test_hash_prints_digests(tmp_path, capsys):
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    assert cli.main(["hash", str(f)]) == 0
    out = capsys.readouterr().out
    assert "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" in out  # sha256("abc")
    assert cli.main(["hash", str(tmp_path / "missing")]) == 1


def test_usage_errors_exit_2():
    with pytest.raises(SystemExit) as exc:
        cli.main(["push", "carrier-pigeon", "f", "h"])
    assert exc.value.code == 2


def test_push_scp_with_password_env(ssh_stub, tmp_path, monkeypatch, capsys):
    port, state = ssh_stub
    f = src(tmp_path)
    monkeypatch.setenv("NT_PW", "secret")
    code = cli.main(["push", "scp", str(f), "127.0.0.1", "-u", "admin", "--password-env", "NT_PW", "--port", str(port)])
    out = capsys.readouterr().out
    assert code == 0 and state["received"] == f.read_bytes()
    assert "SUCCESS" in out and "secret" not in out


def test_push_wrong_password_exits_1(ssh_stub, tmp_path, monkeypatch, capsys):
    port, _ = ssh_stub
    monkeypatch.setenv("NT_PW", "nope")
    code = cli.main(
        ["push", "scp", str(src(tmp_path)), "127.0.0.1", "-u", "admin", "--password-env", "NT_PW", "--port", str(port)]
    )
    assert code == 1 and "Authentication failed" in capsys.readouterr().out


def test_missing_password_env_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NT_NOPE", raising=False)
    with pytest.raises(SystemExit, match="NT_NOPE"):
        cli.main(["push", "scp", str(src(tmp_path)), "127.0.0.1", "-u", "a", "--password-env", "NT_NOPE"])


def test_batch_mixed_results_and_csv(ssh_stub, tmp_path, monkeypatch, capsys):
    port, state = ssh_stub
    dead = free_port(socket.SOCK_STREAM)
    targets = tmp_path / "t.csv"
    targets.write_text(f"host,port\n127.0.0.1,{port}\n127.0.0.1,{dead}\n")
    results = tmp_path / "res.csv"
    f = src(tmp_path)
    monkeypatch.setenv("NT_PW", "secret")
    code = cli.main(
        [
            "batch",
            "scp",
            str(f),
            "--targets",
            str(targets),
            "-u",
            "admin",
            "--password-env",
            "NT_PW",
            "--workers",
            "2",
            "--results",
            str(results),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1  # one device failed
    assert state["received"] == f.read_bytes()  # the other one succeeded
    assert "[ OK ] 127.0.0.1" in out and "[FAIL] 127.0.0.1" in out
    lines = results.read_text().splitlines()
    assert (
        lines[0] == "host,status,detail"
        and sum(",done," in line for line in lines) == 1
        and sum(",failed," in line for line in lines) == 1
    )


def test_batch_bad_csv_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("host,port\n1.1.1.1,notaport\n")
    assert cli.main(["batch", "scp", str(src(tmp_path)), "--targets", str(bad), "-u", "a"]) == 2
    assert "invalid port" in capsys.readouterr().err


def test_connection_test_command(ssh_stub, capsys):
    port, _ = ssh_stub
    assert cli.main(["test", "scp", "127.0.0.1", "--port", str(port)]) == 0
    assert "Host key" in capsys.readouterr().out


def _run_serve(args, tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "serve", *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines, deadline = [], time.time() + 10
    while time.time() < deadline:
        line = proc.stdout.readline()
        lines.append(line)
        if "listening" in line or proc.poll() is not None:
            break
    return proc, lines


def test_cli_serve_tftp_once_exits_cleanly(tmp_path):
    port, f = free_port(socket.SOCK_DGRAM), src(tmp_path)
    proc, lines = _run_serve(["tftp", str(f), "--ip", "127.0.0.1", "--port", str(port), "--once"], tmp_path)
    assert any("listening" in line for line in lines), lines
    out = tmp_path / "got.bin"
    tftpy.TftpClient("127.0.0.1", port).download("whatever", str(out))
    assert out.read_bytes() == f.read_bytes()
    assert proc.wait(15) == 0
    assert "Serve-once" in proc.stdout.read()


def test_cli_serve_http_idle_exits(tmp_path):
    port, f = free_port(socket.SOCK_STREAM), src(tmp_path)
    proc, lines = _run_serve(["http", str(f), "--ip", "127.0.0.1", "--port", str(port), "--idle", "1"], tmp_path)
    assert any("listening" in line for line in lines), lines
    assert proc.wait(15) == 0
    assert "Idle for" in proc.stdout.read()


def test_cli_serve_reports_bind_failure(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        proc, lines = _run_serve(
            ["tftp", str(src(tmp_path)), "--ip", "127.0.0.1", "--port", str(blocker.getsockname()[1])], tmp_path
        )
        assert proc.wait(10) == 1
        assert "already in use" in "".join(lines) + proc.stdout.read()
