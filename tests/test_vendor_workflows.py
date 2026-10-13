"""Spec phases 1 and 2: free-space parsers per vendor, vendor command templates, digest comparison, boot/reload hooks."""

import hashlib
import os

import pytest

from app import cli
from app.core.device_hints import find_hints, hints_for, render_hint
from app.core.events import State
from app.core.install import InstallError, InstallOptions, describe, plan
from app.core.known_hosts import KnownHostsStore
from app.core.preflight import parse_free_bytes
from app.core.scp_service import ScpRequest, ScpService
from app.core.vendors import VENDORS, get_vendor, vendor_from_platform
from app.core.verify import (
    STATUS_ERROR,
    STATUS_MISMATCH,
    STATUS_VERIFIED,
    VerifyOptions,
    check_verify_template,
    evaluate_output,
    parse_digest,
)

# --------------------------------------------------------------------------- free space, per vendor CLI output
CISCO_IOS = """Directory of flash:/

    2  -rw-    99833712  Jan 12 2024 10:52:02 +00:00  c2960x-universalk9-mz.152-7.E9.bin
    3  -rw-        2096  Jan 12 2024 10:52:02 +00:00  config.text

122185728 bytes total (21233664 bytes free)
"""
CISCO_IOSXE = "Directory of bootflash:/\n\n7897481216 bytes total (4916736000 bytes free)\n"
NXOS = """Usage for bootflash://sup-local
 1730998272 bytes used
 1713287168 bytes free
 3444285440 bytes total
"""
ARISTA = """Directory of flash:/

       -rwx   612319573           Mar 14 12:01  EOS-4.30.5M.swi
       -rwx         159           Mar 14 12:02  boot-config

4017795072 bytes total (2985349120 bytes free)
"""
JUNOS = """Filesystem              Size       Used      Avail  Capacity   Mounted on
/dev/gpt/junos          2.5G       1.1G       1.2G       47%  /
devfs                   1.0K       1.0K         0B      100%  /dev
/dev/gpt/var             22G       2.1G        18G       10%  /var
/dev/md1                1.0G        88M       820M       10%  /tmp
"""
LINUX = "Filesystem     1024-blocks    Used Available Capacity Mounted on\n/dev/sda1         41152736 9876543  29204193      26% /\n"
GIB = 1024**3


@pytest.mark.parametrize(
    ("kind", "output", "directory", "expected"),
    [
        ("bytes-free", CISCO_IOS, "flash:", 21233664),
        ("bytes-free", CISCO_IOSXE, "bootflash:", 4916736000),
        ("bytes-free", NXOS, "bootflash:", 1713287168),
        ("bytes-free", ARISTA, "flash:", 2985349120),
        ("junos-storage", JUNOS, "/var", 18 * GIB),
        ("junos-storage", JUNOS, "/var/tmp", 18 * GIB),  # no /var/tmp mount: falls back to the enclosing /var
        ("junos-storage", JUNOS, "/tmp", 820 * 1024**2),  # the longest matching mount wins
        ("junos-storage", JUNOS, "/", int(1.2 * GIB)),
        ("junos-storage", JUNOS, ".", 18 * GIB),  # a relative target lands in the user's home, under /var
        ("df", LINUX, "/", 29204193 * 1024),
        ("bytes-free", "% Invalid input detected at '^' marker.", "flash:", None),
        ("junos-storage", "error: invalid argument", "/var", None),
        ("junos-storage", "Filesystem Size Used Avail Capacity Mounted on\n", "/var", None),
        ("df", "df: /nope: No such file or directory", "/nope", None),
        ("unknown-kind", CISCO_IOS, "flash:", None),
    ],
)
def test_free_space_parsers_across_vendor_outputs(kind, output, directory, expected):
    assert parse_free_bytes(kind, output, directory) == expected


def test_every_vendor_with_a_space_command_names_a_known_parser():
    for vendor in VENDORS.values():
        if vendor.space_cmd:
            assert vendor.space_parser in ("bytes-free", "df", "junos-storage"), vendor.id
            assert parse_free_bytes(vendor.space_parser, "", "/") is None  # an empty reply is "unknown", never a crash


def push(lab, f, tmp_path, **kw):
    events = []
    kw.setdefault("remote_path", "flash:")
    ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(
        ScpRequest("127.0.0.1", "admin", str(f), password="secret", port=lab.port, **kw)
    )
    return events


def image(tmp_path, name="fw.swi", size=50_000):
    f = tmp_path / name
    f.write_bytes(os.urandom(size))
    return f


def test_junos_preflight_uses_the_spec_command_and_falls_back_when_it_takes_no_argument(ssh_lab, tmp_path):
    def junos(command):
        if command == "show system storage /var":
            return "error: syntax error, expecting <command>\n", 0
        if command == "show system storage":
            return JUNOS.replace("18G", "3.0K"), 0  # /var is nearly full
        return "", 0

    ssh_lab.commands = junos
    events = push(
        ssh_lab, image(tmp_path, "junos.tgz"), tmp_path, vendor="junos", remote_path="/var/tmp/", preflight=True
    )
    assert ssh_lab.executed[:2] == ["show system storage /var", "show system storage"]
    assert events[-1].state is State.FAILED and "Not enough space" in events[-1].detail and ssh_lab.files == {}


def test_junos_preflight_passes_with_room(ssh_lab, tmp_path):
    ssh_lab.commands = lambda command: (JUNOS, 0)
    events = push(
        ssh_lab, image(tmp_path, "junos.tgz"), tmp_path, vendor="junos", remote_path="/var/tmp/", preflight=True
    )
    assert events[-1].state is State.FINISHED and ssh_lab.executed[0] == "show system storage /var"


def test_arista_preflight_and_sha256_verification(ssh_lab, tmp_path):
    f = image(tmp_path)

    def eos(command):
        if command == "dir flash:":
            return ARISTA, 0
        if command.startswith("verify /sha256"):
            digest = hashlib.sha256(ssh_lab.files["fw.swi"]).hexdigest()
            return f"verify /sha256 (flash:fw.swi) = {digest}\n", 0
        return "", 0

    ssh_lab.commands = eos
    events = push(ssh_lab, f, tmp_path, vendor="arista-eos", preflight=True, verify=VerifyOptions())
    assert events[-1].state is State.FINISHED and events[-1].detail == STATUS_VERIFIED
    assert ssh_lab.executed == ["dir flash:", "scp -t flash:", "verify /sha256 flash:fw.swi"]


def test_arista_sha256_mismatch_is_reported_as_a_checksum_mismatch(ssh_lab, tmp_path):
    ssh_lab.commands = lambda command: ("verify /sha256 (flash:fw.swi) = " + "a" * 64 + "\n", 0)
    events = push(ssh_lab, image(tmp_path), tmp_path, vendor="arista-eos", verify=VerifyOptions())
    assert events[-1].state is State.FAILED and events[-1].detail.startswith(STATUS_MISMATCH)


# --------------------------------------------------------------------------- hash comparison
H_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
H_256 = hashlib.sha256(b"x").hexdigest()


@pytest.mark.parametrize(
    ("output", "local", "algo", "status"),
    [
        (f".....Done!\nverify /md5 (flash:fw.bin) = {H_MD5}\n", H_MD5, "md5", STATUS_VERIFIED),  # Cisco
        (f"verify /sha256 (flash:fw.swi) = {H_256}\n", H_256, "sha256", STATUS_VERIFIED),  # Arista
        (f"MD5 (/var/tmp/fw.tgz) = {H_MD5}\n", H_MD5, "md5", STATUS_VERIFIED),  # Junos
        (f"{H_MD5.upper()}\n", H_MD5, "md5", STATUS_VERIFIED),  # devices differ in case
        (f"  {H_MD5}  \r\n", H_MD5.upper(), "md5", STATUS_VERIFIED),  # ...and the local side may be upper-case
        ("verify /md5 (flash:fw.bin) = " + "0" * 32, H_MD5, "md5", STATUS_MISMATCH),
        (f"verify /sha256 (flash:x) = {H_256}", H_MD5, "md5", STATUS_ERROR),  # a 64-hex digest is not an md5
        (f"{H_MD5[:30]}\n", H_MD5, "md5", STATUS_ERROR),  # truncated
        ("% Invalid input detected at '^' marker.\n", H_MD5, "md5", STATUS_ERROR),
        ("", H_MD5, "md5", STATUS_ERROR),
    ],
)
def test_digest_comparison_statuses(output, local, algo, status):
    result = evaluate_output(output, local, algo)
    assert result.status == status and result.ok == (status == STATUS_VERIFIED)
    assert result.mismatch == (status == STATUS_MISMATCH)  # only a real digest that differs counts as corruption


def test_comparison_details_name_both_digests_and_never_claim_a_match_on_garbage():
    bad = evaluate_output("x = " + "0" * 32, H_MD5, "md5")
    assert "0" * 32 in bad.detail and H_MD5 in bad.detail and "corrupt" in bad.detail
    assert not evaluate_output("no digest here", H_MD5, "md5").ok


def test_parse_digest_picks_the_last_full_length_hex_string():
    path_like = "f" * 64  # a 64-hex file name must not be mistaken for an md5 or beat the real digest
    assert parse_digest(f"verify /md5 (flash:{path_like}) = {H_MD5}", "md5") == H_MD5
    assert parse_digest(f"{H_MD5}{H_MD5}", "md5") is None  # 64 hex chars in a row is not an md5
    assert parse_digest("sha512 " + "b" * 128, "sha512") == "b" * 128


@pytest.mark.parametrize(
    ("template", "problem"),
    [
        ("md5sum {path}", None),
        ("md5sum {file}", None),
        ("md5sum", "must contain"),
        ("md5sum {nope}", "{nope}"),
        ("md5sum {", "malformed"),
        ("md5sum {0}", "{0}"),
        ("md5sum {}", "(empty)"),
    ],
)
def test_verify_template_validation(template, problem):
    reason = check_verify_template(template)
    assert (reason is None) if problem is None else (problem in reason)


# --------------------------------------------------------------------------- vendor command templates
SERVER = {"ip": "10.0.0.5", "port": 8080}


def render_for(protocol, vendor, scheme=None, port=8080):
    (hint,) = [h for h in hints_for(protocol) if h.vendor == vendor]
    return render_hint(hint, scheme or protocol, SERVER["ip"], port, "fw.swi")


def test_arista_templates():
    assert render_for("tftp", "Arista EOS") == ("copy tftp://10.0.0.5/fw.swi flash:", "verify /sha256 flash:fw.swi")
    assert render_for("http", "Arista EOS", "http")[0] == "copy http://10.0.0.5:8080/fw.swi flash:"
    assert render_for("http", "Arista EOS", "https", 8443)[0] == "copy https://10.0.0.5:8443/fw.swi flash:"


def test_mikrotik_template():
    pull, verify = render_for("http", "MikroTik RouterOS", "http")
    assert pull == '/tool fetch url="http://10.0.0.5:8080/fw.swi" mode=http' and verify is None
    assert render_for("http", "MikroTik RouterOS", "https", 8443)[0].endswith("mode=https")
    assert "check-certificate=no" in [h for h in hints_for("http") if h.vendor == "MikroTik RouterOS"][0].note


def test_aruba_and_palo_alto_templates():
    assert render_for("tftp", "Aruba AOS-CX")[0] == "copy tftp://10.0.0.5/fw.swi primary"
    assert render_for("tftp", "Aruba AOS-CX (SFTP pull)")[0] == "copy sftp://USER@10.0.0.5/fw.swi primary"
    assert render_for("http", "Palo Alto PAN-OS (SCP pull)")[0] == "scp import software from USER@10.0.0.5:fw.swi"
    for h in find_hints("tftp", "palo") + find_hints("tftp", "sftp"):
        assert "SSH server" in h.note  # the caveat that NetTransfer itself does not serve SCP/SFTP travels with them


def test_every_template_renders_for_every_protocol():
    for protocol in ("tftp", "http", "https"):
        for hint in hints_for(protocol):
            pull, verify = render_hint(hint, protocol, "10.0.0.5", 8080, "f.bin")
            assert pull and "{" not in pull and (verify is None or "{" not in verify), hint.vendor


def test_find_hints_matches_loosely():
    assert [h.vendor for h in find_hints("http", "mikro")] == ["MikroTik RouterOS"]
    assert [h.vendor for h in find_hints("http", "arista eos")] == ["Arista EOS"]
    assert find_hints("tftp", "nope") == () and len(find_hints("tftp")) == len(hints_for("tftp"))


def test_cli_commands_subcommand_and_serve_flag(capsys, tmp_path):
    assert cli.main(["commands", "http", "/some/dir/fw.swi", "--ip", "10.0.0.5", "--vendor", "arista"]) == 0
    out = capsys.readouterr().out
    assert (
        "# Arista EOS" in out
        and "copy http://10.0.0.5:8080/fw.swi flash:" in out
        and "verify /sha256 flash:fw.swi" in out
    )
    assert cli.main(["commands", "https", "fw", "--ip", "10.0.0.5", "--port", "8443", "--vendor", "mikrotik"]) == 0
    assert 'url="https://10.0.0.5:8443/fw" mode=https' in capsys.readouterr().out
    assert cli.main(["commands", "tftp", "fw.bin", "--ip", "10.0.0.5"]) == 0
    assert "# Aruba AOS-CX" in capsys.readouterr().out
    assert cli.main(["commands", "http", "fw", "--vendor", "zzz"]) == 2
    assert "no command template matches" in capsys.readouterr().err


# --------------------------------------------------------------------------- boot variable and reload hooks
def test_plan_boot_only_reload_only_and_both():
    ios = get_vendor("cisco-ios")
    boot = plan(ios, "flash:fw.bin", "fw.bin", InstallOptions())
    assert boot == ["configure terminal", "no boot system", "boot system flash:fw.bin", "end", "write memory"]
    assert plan(ios, "flash:fw.bin", "fw.bin", InstallOptions(set_boot=False, reload=True)) == ["reload", ""]
    both = plan(ios, "flash:fw.bin", "fw.bin", InstallOptions(reload_in=5))
    assert both == [*boot, "reload in 5", ""]


@pytest.mark.parametrize(
    ("vendor", "minutes", "expected"),
    [
        ("cisco-ios", 5, ["reload in 5", ""]),
        ("arista-eos", 5, ["reload in 0:05", ""]),
        ("arista-eos", 90, ["reload in 1:30", ""]),
        ("junos", 10, ["request system reboot in 10", "yes"]),
    ],
)
def test_scheduled_reload_templates(vendor, minutes, expected):
    assert plan(get_vendor(vendor), "x", "x", InstallOptions(set_boot=False, reload_in=minutes)) == expected


@pytest.mark.parametrize(
    ("vendor", "opts", "message"),
    [
        ("cisco-nxos", InstallOptions(set_boot=False, reload_in=5), "scheduled reload is not available"),
        ("huawei", InstallOptions(set_boot=False, reload_in=5), "scheduled reload is not available"),
        ("cisco-ios", InstallOptions(reload=True, reload_in=5), "not both"),
        ("cisco-ios", InstallOptions(set_boot=False, reload_in=0), "between 1 and"),
        ("cisco-ios", InstallOptions(set_boot=False, reload_in=24 * 60 + 1), "between 1 and"),
        ("cisco-ios", InstallOptions(set_boot=False), "nothing to do"),
        ("fortinet", InstallOptions(), "no boot-image profile"),
        ("linux", InstallOptions(set_boot=False, reload=True), "no reload command"),
    ],
)
def test_plan_refuses_what_a_vendor_cannot_do(vendor, opts, message):
    with pytest.raises(InstallError, match=message):
        plan(get_vendor(vendor), "flash:fw.bin", "fw.bin", opts)


def test_plan_refuses_unsafe_file_names_for_boot_commands():
    with pytest.raises(InstallError, match="unsafe"):
        plan(get_vendor("cisco-ios"), "flash:x;reload", "x;reload", InstallOptions())
    assert plan(get_vendor("cisco-ios"), "flash:x;reload", "x;reload", InstallOptions(set_boot=False, reload=True))


def test_describe_summaries():
    assert describe(InstallOptions()) == "set the boot image to the pushed file"
    assert "schedule a reload in 7 minute(s)" in describe(InstallOptions(reload_in=7))
    assert "RELOAD the device immediately" in describe(InstallOptions(set_boot=False, reload=True))


def verified_lab(lab):
    def handler(command):
        if command.startswith("verify /md5"):
            name = command.rsplit(":", 1)[-1]
            return f"verify /md5 ({command.split()[-1]}) = {hashlib.md5(lab.files[name]).hexdigest()}\n", 0
        return "", 0

    lab.commands = handler


def test_execute_needs_explicit_confirmation_in_the_core(ssh_lab, tmp_path):
    verified_lab(ssh_lab)
    events = push(
        ssh_lab,
        image(tmp_path, "fw.bin"),
        tmp_path,
        vendor="cisco-ios",
        verify=VerifyOptions(),
        install=InstallOptions(execute=True),  # not confirmed
    )
    assert events[-1].state is State.FAILED and "explicit confirmation" in events[-1].detail
    assert ssh_lab.files == {} and ssh_lab.shell_log == []  # refused before anything was sent or typed


def test_scheduled_reload_is_executed_and_the_session_is_not_treated_as_lost(ssh_lab, tmp_path):
    verified_lab(ssh_lab)
    opts = InstallOptions(execute=True, set_boot=False, reload_in=5, confirmed=True, timeout=10)
    events = push(
        ssh_lab, image(tmp_path, "fw.bin"), tmp_path, vendor="cisco-ios", verify=VerifyOptions(), install=opts
    )
    assert events[-1].state is State.FINISHED
    assert ssh_lab.shell_log == ["reload in 5", ""]  # no boot-variable commands: the hooks are independent


def test_reload_only_dry_run_changes_nothing(ssh_lab, tmp_path):
    verified_lab(ssh_lab)
    opts = InstallOptions(set_boot=False, reload_in=15)
    events = push(
        ssh_lab, image(tmp_path, "fw.bin"), tmp_path, vendor="cisco-ios", verify=VerifyOptions(), install=opts
    )
    text = " | ".join(e.message for e in events if hasattr(e, "message"))
    assert "reload in 15" in text and "dry run" in text and ssh_lab.shell_log == []


def test_an_impossible_plan_fails_before_anything_is_uploaded(ssh_lab, tmp_path):
    verified_lab(ssh_lab)
    opts = InstallOptions(set_boot=False, reload_in=5)  # NX-OS has no scheduled reload
    events = push(
        ssh_lab, image(tmp_path, "fw.bin"), tmp_path, vendor="cisco-nxos", remote_path="bootflash:", install=opts
    )
    assert events[-1].state is State.FAILED and "scheduled reload is not available" in events[-1].detail
    assert ssh_lab.files == {}


def test_arista_boot_and_reload_plan_runs_end_to_end(ssh_lab, tmp_path):
    f = image(tmp_path)

    def eos(command):
        if command.startswith("verify /sha256"):
            return f"verify /sha256 (flash:fw.swi) = {hashlib.sha256(ssh_lab.files['fw.swi']).hexdigest()}\n", 0
        return "", 0

    ssh_lab.commands = eos
    opts = InstallOptions(execute=True, reload_in=30, confirmed=True, timeout=10)
    events = push(ssh_lab, f, tmp_path, vendor="arista-eos", verify=VerifyOptions(), install=opts)
    assert events[-1].state is State.FINISHED
    assert ssh_lab.shell_log == ["configure", "boot system flash:fw.swi", "end", "write memory", "reload in 0:30", ""]


# --------------------------------------------------------------------------- CLI confirmation
@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_store", lambda: KnownHostsStore(tmp_path / "known_hosts"))
    monkeypatch.setenv("NT_PW", "secret")


def cli_push(lab, f, *extra):
    return [
        "push", "scp", str(f), "127.0.0.1", "-u", "admin", "--password-env", "NT_PW", "--port", str(lab.port),
        "--remote", "flash:", "--vendor", "cisco-ios", "--verify", *extra,
    ]  # fmt: skip


def test_cli_prompts_before_executing_and_aborts_on_anything_but_yes(cli_env, ssh_lab, tmp_path, monkeypatch, capsys):
    verified_lab(ssh_lab)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    answers = iter(["no"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    with pytest.raises(SystemExit, match="Aborted: not confirmed"):
        cli.main(cli_push(ssh_lab, image(tmp_path, "fw.bin"), "--install", "--execute"))
    assert "About to change 127.0.0.1" in capsys.readouterr().err
    assert ssh_lab.files == {} and ssh_lab.shell_log == []

    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert cli.main(cli_push(ssh_lab, image(tmp_path, "fw.bin"), "--reload-in", "5", "--execute")) == 0
    assert ssh_lab.shell_log == ["reload in 5", ""]


def test_cli_refuses_to_execute_without_a_terminal_unless_yes(cli_env, ssh_lab, tmp_path, monkeypatch):
    verified_lab(ssh_lab)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="--yes"):
        cli.main(cli_push(ssh_lab, image(tmp_path, "fw.bin"), "--install", "--execute"))
    assert cli.main(cli_push(ssh_lab, image(tmp_path, "fw.bin"), "--install", "--execute", "--yes")) == 0
    assert "boot system flash:fw.bin" in ssh_lab.shell_log


def test_cli_dry_runs_never_prompt(cli_env, ssh_lab, tmp_path, monkeypatch, capsys):
    verified_lab(ssh_lab)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("a dry run must not ask for confirmation"))
    assert cli.main(cli_push(ssh_lab, image(tmp_path, "fw.bin"), "--set-boot", "--reload-in", "10")) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "reload in 10" in out and ssh_lab.shell_log == []


@pytest.mark.parametrize(
    ("platform", "vendor"),
    [
        ("cisco-ios", "cisco-ios"),
        ("Cisco IOS-XE", "cisco-ios"),
        ("cisco-nxos", "cisco-nxos"),
        ("NX-OS", "cisco-nxos"),
        ("cisco-ios-xr", ""),  # a different OS: never guessed as IOS
        ("arista-eos", "arista-eos"),
        ("EOS", "arista-eos"),
        ("juniper-junos", "junos"),
        ("Junos", "junos"),
        ("huawei-vrp", "huawei"),
        ("fortinet-fortios", "fortinet"),
        ("ubuntu-linux", "linux"),
        ("mikrotik-routeros", ""),
        ("", ""),
    ],
)
def test_platform_to_vendor_mapping(platform, vendor):
    assert vendor_from_platform(platform) == vendor
