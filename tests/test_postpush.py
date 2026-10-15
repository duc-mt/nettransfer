"""Post-push features against the in-process SSH lab device: preflight, verify, install, jump host, resume."""

import hashlib
import os
from pathlib import Path

import pytest

from app.core.events import Level, LogEvent, State, StateEvent
from app.core.install import InstallOptions
from app.core.known_hosts import KnownHostsStore
from app.core.scp_service import ScpRequest, ScpService
from app.core.ssh_session import JumpHost, parse_jump
from app.core.vendors import UnknownVendorError, device_path, get_vendor, parent_dir
from app.core.verify import VerifyOptions, parse_digest


def make_image(tmp_path, name="fw.bin", size=120_000):
    f = tmp_path / name
    f.write_bytes(os.urandom(size))
    return f


def md5_of(lab, name):
    data = lab.files.get(name)
    if data is None:
        data = Path(os.path.join(lab.root, name)).read_bytes()
    return hashlib.md5(data).hexdigest()


def cisco_verify(lab, corrupt=False):
    def handler(command):
        if command.startswith("verify /md5"):
            name = command.rsplit(":", 1)[-1]
            digest = md5_of(lab, name)
            if corrupt:
                digest = "0" * 32
            return f".....Done!\nverify /md5 ({command.split()[-1]}) = {digest}\n", 0
        return "", 0

    return handler


def push(lab, f, tmp_path, **kw):
    events = []
    kw.setdefault("remote_path", "flash:")
    ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(
        ScpRequest("127.0.0.1", "admin", str(f), password="secret", port=lab.port, **kw)
    )
    return events


def texts(events):
    return " | ".join(e.message for e in events if isinstance(e, LogEvent))


# ------------------------------------------------------------------ small helpers
def test_vendor_lookup_aliases_and_errors():
    assert get_vendor("Cisco").id == "cisco-ios" and get_vendor("nxos").id == "cisco-nxos"
    with pytest.raises(UnknownVendorError, match="known:"):
        get_vendor("acme")


def test_device_path_and_parent_dir():
    ios = get_vendor("cisco-ios")
    assert device_path("", "a.bin", ios) == "flash:a.bin"
    assert device_path("flash:", "a.bin", ios) == "flash:a.bin"
    assert device_path("/var/tmp/", "a.tgz", None) == "/var/tmp/a.tgz"
    assert device_path("/var/tmp/renamed.tgz", "a.tgz", None) == "/var/tmp/renamed.tgz"
    assert parent_dir("flash:a.bin") == "flash:" and parent_dir("/var/tmp/x") == "/var/tmp" and parent_dir("x") == "."


def test_parse_digest_takes_the_last_match():
    other = "a" * 64
    out = f"verify /md5 (flash:{other}.bin) = {'b' * 32}\n"
    assert parse_digest(out, "md5") == "b" * 32
    assert parse_digest("nothing here", "md5") is None


def test_parse_jump():
    j = parse_jump("ops@bastion.example:2222")
    assert (j.username, j.host, j.port) == ("ops", "bastion.example", 2222)
    assert parse_jump("bastion").port == 22
    with pytest.raises(ValueError):
        parse_jump("a@h:notaport")


# ------------------------------------------------------------------ verify
def test_verify_ok_after_scp(ssh_lab, tmp_path):
    ssh_lab.commands = cisco_verify(ssh_lab)
    f = make_image(tmp_path)
    events = push(ssh_lab, f, tmp_path, vendor="cisco-ios", verify=VerifyOptions())
    assert events[-1].state is State.FINISHED and events[-1].detail == "VERIFIED (hash match)"
    assert "verify /md5 flash:fw.bin" in ssh_lab.executed
    assert any(e.tag == "verify" for e in events if isinstance(e, LogEvent))


def test_verify_mismatch_fails_and_is_retryable(ssh_lab, tmp_path):
    ssh_lab.commands = cisco_verify(ssh_lab, corrupt=True)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", verify=VerifyOptions())
    last = events[-1]
    assert (
        last.state is State.FAILED and last.detail.startswith("FAILED (checksum mismatch)") and last.transient is True
    )


def test_verify_unreadable_reply_fails(ssh_lab, tmp_path):
    ssh_lab.commands = lambda command: ("% Invalid input detected\n", 0)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", verify=VerifyOptions())
    assert events[-1].state is State.FAILED and "could not read" in events[-1].detail


def test_verify_without_known_command_asks_for_one(ssh_lab, tmp_path):
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="fortinet", remote_path="/", verify=VerifyOptions())
    assert events[-1].state is State.FAILED and "--verify-cmd" in events[-1].detail


def test_custom_verify_command_and_sha256(ssh_lab, tmp_path):
    f = make_image(tmp_path)
    ssh_lab.commands = lambda command: (f"{hashlib.sha256(ssh_lab.files['fw.bin']).hexdigest()}  fw.bin\n", 0)
    events = push(ssh_lab, f, tmp_path, verify=VerifyOptions(command="sha256sum {path}", algo="sha256"))
    assert events[-1].state is State.FINISHED and "sha256sum flash:fw.bin" in ssh_lab.executed


def test_unsafe_file_name_is_never_put_in_a_device_command(ssh_lab, tmp_path):
    f = make_image(tmp_path, name="x;reload.bin")
    events = push(ssh_lab, f, tmp_path, vendor="cisco-ios", verify=VerifyOptions())
    assert events[-1].state is State.FAILED and "refusing" in events[-1].detail
    assert not any(c.startswith("verify") for c in ssh_lab.executed)


# ------------------------------------------------------------------ preflight
def test_preflight_blocks_when_there_is_no_room(ssh_lab, tmp_path):
    ssh_lab.commands = lambda command: ("1000000 bytes total (5000 bytes free)\n", 0)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", preflight=True)
    assert events[-1].state is State.FAILED and "Not enough space" in events[-1].detail
    assert not any(isinstance(e, StateEvent) and e.state is State.STARTED for e in events)  # nothing was sent
    assert ssh_lab.files == {}


def test_preflight_passes_and_warns_about_a_odd_extension(ssh_lab, tmp_path):
    ssh_lab.commands = lambda command: ("9000000000 bytes total (8000000000 bytes free)\n", 0)
    events = push(ssh_lab, make_image(tmp_path, name="fw.txt"), tmp_path, vendor="cisco-ios", preflight=True)
    assert events[-1].state is State.FINISHED
    warnings = [e.message for e in events if isinstance(e, LogEvent) and e.level is Level.WARNING]
    assert any("does not look like" in w for w in warnings)
    assert "dir flash:" in ssh_lab.executed


def test_preflight_unknown_space_never_blocks(ssh_lab, tmp_path):
    ssh_lab.commands = lambda command: ("???\n", 0)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", preflight=True)
    assert events[-1].state is State.FINISHED and "could not read" in texts(events)


# ------------------------------------------------------------------ install
def test_install_defaults_to_a_dry_run(ssh_lab, tmp_path):
    ssh_lab.commands = cisco_verify(ssh_lab)
    events = push(
        ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", verify=VerifyOptions(), install=InstallOptions()
    )
    assert events[-1].state is State.FINISHED
    assert "boot system flash:fw.bin" in texts(events) and "dry run" in texts(events)
    assert ssh_lab.shell_log == []  # nothing typed into the router


def test_install_execute_refuses_without_verification(ssh_lab, tmp_path):
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", install=InstallOptions(execute=True))
    assert events[-1].state is State.FAILED and "unverified" in events[-1].detail
    assert ssh_lab.files == {}  # refused before anything was uploaded


def test_install_execute_runs_the_plan_and_reload_only_when_asked(ssh_lab, tmp_path):
    ssh_lab.commands = cisco_verify(ssh_lab)
    opts = InstallOptions(execute=True, confirmed=True, timeout=10)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", verify=VerifyOptions(), install=opts)
    assert events[-1].state is State.FINISHED
    assert ssh_lab.shell_log == [
        "configure terminal",
        "no boot system",
        "boot system flash:fw.bin",
        "end",
        "write memory",
    ]
    ssh_lab.shell_log.clear()
    ssh_lab.files.clear()
    opts = InstallOptions(execute=True, reload=True, confirmed=True, timeout=10)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", verify=VerifyOptions(), install=opts)
    assert events[-1].state is State.FINISHED and "reload" in ssh_lab.shell_log


def test_install_stops_at_the_first_rejected_command(ssh_lab, tmp_path):
    ssh_lab.commands = cisco_verify(ssh_lab)
    ssh_lab.shell_replies = {"no boot system": "% Invalid input detected at '^' marker."}
    opts = InstallOptions(execute=True, confirmed=True, timeout=10)
    events = push(ssh_lab, make_image(tmp_path), tmp_path, vendor="cisco-ios", verify=VerifyOptions(), install=opts)
    assert events[-1].state is State.FAILED and "rejected" in events[-1].detail
    assert "write memory" not in ssh_lab.shell_log


def test_install_needs_a_vendor_profile(ssh_lab, tmp_path):
    events = push(ssh_lab, make_image(tmp_path), tmp_path, install=InstallOptions())
    assert events[-1].state is State.FAILED and "vendor" in events[-1].detail


# ------------------------------------------------------------------ jump host
def test_push_through_a_jump_host(ssh_lab, tmp_path):
    from tests.conftest import SshLab

    device = SshLab(tmp_path / "dev")
    ssh_lab.allow_forward = True
    try:
        f = make_image(tmp_path)
        events = []
        req = ScpRequest(
            "127.0.0.1",
            "admin",
            str(f),
            password="secret",
            port=device.port,
            remote_path="flash:",
            jump=JumpHost("127.0.0.1", "admin", "secret", ssh_lab.port),
        )
        ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(req)
        assert events[-1].state is State.FINISHED, events[-1]
        assert device.files["fw.bin"] == f.read_bytes()
        assert ("127.0.0.1", device.port) in ssh_lab.forwarded
        assert "jump host" in texts(events)
    finally:
        device.close()


def test_jump_host_that_refuses_forwarding_fails_cleanly(ssh_lab, tmp_path):
    events = []
    req = ScpRequest(
        "127.0.0.1",
        "admin",
        str(make_image(tmp_path)),
        password="secret",
        port=9,
        jump=JumpHost("127.0.0.1", "admin", "secret", ssh_lab.port),
    )
    ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(req)
    assert events[-1].state is State.FAILED


# ------------------------------------------------------------------ resume
def sftp_push(lab, f, tmp_path, **kw):
    events = []
    ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(
        ScpRequest(
            "127.0.0.1", "admin", str(f), password="secret", port=lab.port, protocol="sftp", remote_path="/", **kw
        )
    )
    return events


def test_sftp_resume_continues_a_partial_file(ssh_lab, tmp_path):
    f = make_image(tmp_path, size=200_000)
    partial = os.path.join(ssh_lab.root, "fw.bin")
    with open(partial, "wb") as fh:
        fh.write(f.read_bytes()[:70_000])
    events = sftp_push(ssh_lab, f, tmp_path, resume=True)
    assert events[-1].state is State.FINISHED and "Resuming at" in texts(events)
    assert Path(partial).read_bytes() == f.read_bytes()


def test_sftp_without_resume_starts_over(ssh_lab, tmp_path):
    f = make_image(tmp_path, size=100_000)
    partial = os.path.join(ssh_lab.root, "fw.bin")
    Path(partial).write_bytes(b"junk" * 100)
    events = sftp_push(ssh_lab, f, tmp_path)
    assert events[-1].state is State.FINISHED and "Resuming" not in texts(events)
    assert Path(partial).read_bytes() == f.read_bytes()


def test_resume_discards_a_remote_file_that_is_not_shorter(ssh_lab, tmp_path):
    f = make_image(tmp_path, size=50_000)
    partial = os.path.join(ssh_lab.root, "fw.bin")
    Path(partial).write_bytes(os.urandom(80_000))  # longer than the image: cannot be a prefix
    events = sftp_push(ssh_lab, f, tmp_path, resume=True)
    assert events[-1].state is State.FINISHED
    assert Path(partial).read_bytes() == f.read_bytes()


def test_resume_needs_sftp(ssh_lab, tmp_path):
    events = push(ssh_lab, make_image(tmp_path), tmp_path, resume=True)
    assert events[-1].state is State.FAILED and "SFTP" in events[-1].detail
