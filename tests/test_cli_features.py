"""CLI surface of the new features: option validation, push --verify, inventory batches, audit/history, serve --to."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from app import cli
from app.core.audit import AuditLog, load_or_create_key
from app.core.events import Level, LogEvent
from app.core.known_hosts import KnownHostsStore


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_store", lambda: KnownHostsStore(tmp_path / "known_hosts"))
    monkeypatch.setenv("NT_PW", "secret")
    monkeypatch.setattr("app.platform.paths.data_dir", lambda: tmp_path / "data")


def image(tmp_path, name="fw.bin"):
    f = tmp_path / name
    f.write_bytes(os.urandom(40_000))
    return f


def push_args(lab, f, *extra, protocol="scp"):
    return [
        "push", protocol, str(f), "127.0.0.1", "-u", "admin", "--password-env", "NT_PW", "--port", str(lab.port),
        "--remote", "flash:", *extra,
    ]  # fmt: skip


def digest_handler(lab, wrong=False):
    def handler(command):
        if command.startswith("verify /md5"):
            name = command.rsplit(":", 1)[-1]
            digest = "f" * 32 if wrong else hashlib.md5(lab.files[name]).hexdigest()
            return f"verify /md5 ({command.split()[-1]}) = {digest}\n", 0
        return "", 0

    return handler


# ------------------------------------------------------------------ option validation
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--verify"], "--verify needs --vendor"),
        (["--install"], "--install needs --vendor"),
        (["--vendor", "cisco-ios", "--execute"], "only make sense with --install"),
        (["--vendor", "cisco-ios", "--install", "--execute"], "add --verify"),
        (["--vendor", "cisco-ios", "--install", "--reload"], "--reload needs --execute"),
        (["--vendor", "acme"], "unknown vendor"),
        (["--resume"], "--resume needs sftp"),
    ],
)
def test_push_rejects_inconsistent_options(tmp_path, capsys, extra, message):
    with pytest.raises(SystemExit) as exc:
        cli.main(["push", "scp", str(image(tmp_path)), "127.0.0.1", "-u", "a", "--password-env", "NT_PW", *extra])
    assert message in str(exc.value)


def test_verify_needs_ssh_protocols(tmp_path):
    with pytest.raises(SystemExit, match="scp or sftp"):
        cli.main(
            [
                "push",
                "ftp",
                str(image(tmp_path)),
                "127.0.0.1",
                "--password-env",
                "NT_PW",
                "--vendor",
                "cisco-ios",
                "--verify",
            ]
        )
    with pytest.raises(SystemExit, match="scp or sftp"):
        cli.main(["push", "ftp", str(image(tmp_path)), "127.0.0.1", "--password-env", "NT_PW", "--jump", "b"])


# ------------------------------------------------------------------ push --verify / --install through the CLI
def test_cli_push_verify_ok_and_mismatch(ssh_lab, tmp_path, capsys):
    ssh_lab.commands = digest_handler(ssh_lab)
    f = image(tmp_path)
    assert cli.main(push_args(ssh_lab, f, "--vendor", "cisco-ios", "--verify")) == 0
    assert "Verified fw.bin" in capsys.readouterr().out
    ssh_lab.commands = digest_handler(ssh_lab, wrong=True)
    assert cli.main(push_args(ssh_lab, f, "--vendor", "cisco-ios", "--verify")) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_cli_install_is_a_dry_run_unless_executed(ssh_lab, tmp_path, capsys):
    ssh_lab.commands = digest_handler(ssh_lab)
    f = image(tmp_path)
    assert cli.main(push_args(ssh_lab, f, "--vendor", "cisco-ios", "--verify", "--install")) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "boot system flash:fw.bin" in out and ssh_lab.shell_log == []
    assert cli.main(push_args(ssh_lab, f, "--vendor", "cisco-ios", "--verify", "--install", "--execute")) == 0
    assert "boot system flash:fw.bin" in ssh_lab.shell_log


def test_cli_preflight_and_sftp_resume(ssh_lab, tmp_path, capsys):
    ssh_lab.commands = lambda c: ("100 bytes total (10 bytes free)\n", 0)
    f = image(tmp_path)
    assert cli.main(push_args(ssh_lab, f, "--vendor", "cisco-ios", "--preflight")) == 1
    assert "Not enough space" in capsys.readouterr().out
    with open(os.path.join(ssh_lab.root, "fw.bin"), "wb") as fh:
        fh.write(f.read_bytes()[:10_000])
    args = push_args(ssh_lab, f, "--resume", protocol="sftp")
    args[args.index("flash:")] = "/"
    assert cli.main(args) == 0
    assert "Resuming at" in capsys.readouterr().out
    assert Path(os.path.join(ssh_lab.root, "fw.bin")).read_bytes() == f.read_bytes()


def test_cli_push_through_jump_host(ssh_lab, tmp_path, monkeypatch, capsys):
    from tests.conftest import SshLab

    device = SshLab(tmp_path / "dev")
    ssh_lab.allow_forward = True
    monkeypatch.setenv("NT_JUMP_PW", "secret")
    try:
        f = image(tmp_path)
        args = push_args(device, f, "--jump", f"admin@127.0.0.1:{ssh_lab.port}", "--jump-password-env", "NT_JUMP_PW")
        assert cli.main(args) == 0
        assert device.files["fw.bin"] == f.read_bytes() and ssh_lab.forwarded
        assert "jump host" in capsys.readouterr().out
    finally:
        device.close()


# ------------------------------------------------------------------ batch with inventories
def inventory(tmp_path, ports):
    entries = [
        {"host": "127.0.0.1", "port": ports[0], "username": "admin", "site": "A", "device_type": "cisco_ios"},
        {"host": "127.0.0.1", "port": ports[1], "username": "admin", "site": "B", "device_type": "cisco_ios"},
    ]
    path = tmp_path / "inv.json"
    path.write_text(json.dumps(entries))
    return str(path)


def test_batch_inventory_filter_and_list(ssh_lab, tmp_path, capsys):
    from tests.conftest import SshLab

    other = SshLab(tmp_path / "other")
    try:
        inv = inventory(tmp_path, [ssh_lab.port, other.port])
        f = image(tmp_path)
        assert cli.main(["batch", "scp", str(f), "--inventory", inv, "--filter", "site:A", "--list-targets"]) == 0
        out = capsys.readouterr().out
        assert "1 device(s)" in out and "site:A" in out and "site:B" not in out
        base = ["batch", "scp", str(f), "--inventory", inv, "--password-env", "NT_PW", "--remote", "flash:"]
        assert cli.main([*base, "--filter", "site:A"]) == 0
        assert "fw.bin" in ssh_lab.files and "fw.bin" not in other.files  # only site A was pushed to
        assert cli.main([*base, "--filter", "site:Z"]) == 2
        assert "matches no device" in capsys.readouterr().err
    finally:
        other.close()


def test_batch_needs_exactly_one_source(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["batch", "scp", str(image(tmp_path))])


def test_batch_retry_options_are_accepted(ssh_lab, tmp_path):
    f = image(tmp_path)
    inv = tmp_path / "t.csv"
    inv.write_text(f"host,port,username\n127.0.0.1,{ssh_lab.port},admin\n")
    assert (
        cli.main(
            [
                "batch",
                "scp",
                str(f),
                "--targets",
                str(inv),
                "--password-env",
                "NT_PW",
                "--retries",
                "2",
                "--backoff",
                "0.01",
            ]
        )
        == 0
    )


# ------------------------------------------------------------------ audit / history
def make_logs(tmp_path):
    folder = tmp_path / "data"
    audit = AuditLog(folder / "logs", key=load_or_create_key(folder / "audit.key"))
    audit.write("Push", LogEvent(Level.SUCCESS, "Pushed a.bin to 10.0.0.1 via SCP in 1.0s (1 MB/s avg)"))
    audit.write("Push", LogEvent(Level.SUCCESS, f"Verified a.bin on 10.0.0.1: MD5 {'c' * 32} matches on the device"))
    return audit


def test_audit_command_detects_tampering(tmp_path, capsys):
    audit = make_logs(tmp_path)
    assert cli.main(["audit"]) == 0
    out = capsys.readouterr().out
    assert "[OK" in out and "head " in out
    head = out.split("head ")[1].split()[0]
    assert cli.main(["audit", "--expect-head", head]) == 0
    assert cli.main(["audit", "--expect-head", "0" * 32]) == 1  # a head that does not match = truncated/replaced
    capsys.readouterr()
    audit.path.write_text(audit.path.read_text().replace("a.bin", "b.bin", 1))
    assert cli.main(["audit"]) == 1
    assert "TAMPERED" in capsys.readouterr().out


def test_history_command_text_and_html(tmp_path, capsys):
    make_logs(tmp_path)
    assert cli.main(["history"]) == 0
    assert "10.0.0.1" in capsys.readouterr().out
    out_file = tmp_path / "report.html"
    assert cli.main(["history", "--html", str(out_file), "--device", "10.0.0"]) == 0
    page = out_file.read_text()
    assert "a.bin" in page and "MD5 " + "c" * 32 in page


def test_cli_audit_flag_feeds_history(ssh_lab, tmp_path, capsys):
    ssh_lab.commands = digest_handler(ssh_lab)
    assert cli.main(push_args(ssh_lab, image(tmp_path), "--vendor", "cisco-ios", "--verify", "--audit")) == 0
    capsys.readouterr()
    assert cli.main(["audit"]) == 0
    assert cli.main(["history"]) == 0
    assert "fw.bin" in capsys.readouterr().out


# ------------------------------------------------------------------ serve / interfaces / credentials
def test_serve_option_validation(tmp_path):
    f = image(tmp_path)
    with pytest.raises(SystemExit, match="--cert/--key need"):
        cli.main(["serve", "http", str(f), "--cert", "x.pem"])
    with pytest.raises(SystemExit, match="--key needs --cert"):
        cli.main(["serve", "http", str(f), "--https", "--key", "k.pem"])
    with pytest.raises(SystemExit, match="either --ip or --to"):
        cli.main(["serve", "tftp", str(f), "--ip", "127.0.0.1", "--to", "127.0.0.1"])
    with pytest.raises(SystemExit, match="IPv4 only"):
        cli.main(["serve", "tftp", str(f), "--ip", "::1"])


def test_bind_ip_resolution(monkeypatch):
    class A:
        ip, to = None, None

    a = A()
    a.to = "127.0.0.1"
    assert cli._bind_ip(a) == "127.0.0.1"  # the loopback route answers for itself
    a.to, a.ip = None, "all"
    assert cli._bind_ip(a) == "0.0.0.0"
    a.ip = "10.1.1.1"
    assert cli._bind_ip(a) == "10.1.1.1"


def test_interfaces_command(capsys):
    cli.main(["interfaces", "--to", "127.0.0.1"])
    assert "route to 127.0.0.1" in capsys.readouterr().out


def test_credentials_commands(monkeypatch, capsys):
    from app.core.credentials import CredentialStore
    from tests.test_audit_history_credentials import FakeKeyring

    backend = FakeKeyring()
    monkeypatch.setattr(cli, "CredentialStore", lambda: CredentialStore(backend))
    assert cli.main(["credentials", "set", "10.0.0.1", "admin", "--password-env", "NT_PW"]) == 0
    assert backend.data[("nettransfer", "admin@10.0.0.1")] == "secret"
    assert cli.main(["credentials", "delete", "10.0.0.1", "admin"]) == 0
    assert cli.main(["credentials", "delete", "10.0.0.1", "admin"]) == 0
    assert "Nothing stored" in capsys.readouterr().out


def test_push_uses_the_keyring_with_use_keyring(ssh_lab, tmp_path, monkeypatch):
    from app.core.credentials import CredentialStore
    from tests.test_audit_history_credentials import FakeKeyring

    backend = FakeKeyring()
    backend.set_password("nettransfer", "admin@*", "secret")
    monkeypatch.setattr(cli, "CredentialStore", lambda: CredentialStore(backend))
    f = image(tmp_path)
    args = [
        "push",
        "scp",
        str(f),
        "127.0.0.1",
        "-u",
        "admin",
        "--port",
        str(ssh_lab.port),
        "--use-keyring",
        "--remote",
        "flash:",
    ]
    assert cli.main(args) == 0  # no password prompt, no env var: it came from the keyring
    assert ssh_lab.files["fw.bin"] == f.read_bytes()
