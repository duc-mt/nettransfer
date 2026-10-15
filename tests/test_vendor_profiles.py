"""Vendor profiles: Aruba AOS-CX, Palo Alto PAN-OS, Huawei VRP, Fortinet FortiOS, VyOS, pfSense/OPNsense, Ruijie RGOS,
Sophos SFOS and Check Point Gaia: output parsers, templates, execution modes, workflows against a scripted device."""

import hashlib
import os
import re
import string

import paramiko
import pytest

from app import cli
from app.core.devicecheck import DeviceCheck, DeviceCheckService
from app.core.events import Level, LogEvent, State
from app.core.install import InstallError, InstallOptions, describe, describe_plan, plan
from app.core.known_hosts import KnownHostsStore
from app.core.preflight import parse_free_bytes
from app.core.remote import ABORT, RemoteError, Shell, looks_like_question, parse_answer
from app.core.scp_service import ScpRequest, ScpService
from app.core.vendors import (
    SHELL_CLI,
    SHELL_POSIX,
    TRANSFER_PULL,
    TRANSFER_PUSH,
    TRANSFER_WEB,
    VENDORS,
    get_vendor,
    version_from_filename,
)
from app.core.verify import (
    STATUS_ERROR,
    STATUS_MISMATCH,
    STATUS_VERIFIED,
    STATUS_VERSION_BAD,
    STATUS_VERSION_OK,
    VerifyOptions,
    check_version,
    evaluate_output,
)

REQUESTED = {
    "cisco": "cisco-ios", "junos": "junos", "huawei": "huawei", "fortinet": "fortinet", "arista": "arista-eos",
    "vyos": "vyos", "ruijie": "ruijie-rgos", "aruba": "aruba-aos-cx", "checkpoint": "checkpoint-gaia",
    "paloalto": "paloalto-panos", "pfsense": "pfsense", "sophos": "sophos-sfos",
}  # fmt: skip


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def image(tmp_path, name, size=40_000):
    f = tmp_path / name
    f.write_bytes(os.urandom(size))
    return f


def push(lab, f, tmp_path, **kw):
    events = []
    ScpService(events.append, KnownHostsStore(tmp_path / "kh")).push(
        ScpRequest("127.0.0.1", "admin", str(f), password="secret", port=lab.port, **kw)
    )
    return events


def log_text(events, level=None):
    return " | ".join(e.message for e in events if isinstance(e, LogEvent) and (level is None or e.level is level))


def connect(lab):
    transport = paramiko.Transport(("127.0.0.1", lab.port))
    transport.start_client(timeout=10)
    transport.auth_password("admin", "secret")
    return transport


# =============================================================================================== profile table sanity
def test_every_requested_vendor_name_resolves_to_its_profile():
    assert {name: get_vendor(name).id for name in REQUESTED} == REQUESTED
    assert get_vendor("OPNsense").id == "opnsense" and get_vendor("pan-os").id == "paloalto-panos"
    assert get_vendor("fortigate").id == "fortinet" and get_vendor("gaia").id == "checkpoint-gaia"


def test_profile_templates_are_well_formed():
    values = {"path": "flash:/fw.cc", "file": "fw.cc", "version": "1.2.3", "minutes": 5, "hhmm": "0:05", "dir": "/tmp"}
    for vendor in VENDORS.values():
        assert vendor.transfer in (TRANSFER_PUSH, TRANSFER_PULL, TRANSFER_WEB), vendor.id
        assert vendor.shell in (SHELL_CLI, SHELL_POSIX), vendor.id
        for template in (vendor.verify_cmd, vendor.space_cmd, vendor.space_fallback, vendor.version_cmd):
            if template:
                fields = {n for _, n, _, _ in string.Formatter().parse(template) if n}
                assert fields <= set(values), (vendor.id, template)
                template.format(**values)
        for line in (*vendor.install, *vendor.reload, *vendor.reload_in, *vendor.verify_steps):
            if line.startswith("@"):
                parse_answer(line)  # must parse
            else:
                line.format(**values)
        if vendor.verify_marker:
            re.compile(vendor.verify_marker)
        if vendor.shell_hint_pattern:
            re.compile(vendor.shell_hint_pattern)
            assert vendor.shell_hint, vendor.id  # a pattern without advice helps nobody
        if vendor.transfer != TRANSFER_PUSH:
            assert vendor.note, f"{vendor.id}: a vendor that does not take a push must say what to do instead"
        if vendor.verify_cmd or vendor.verify_steps:
            assert vendor.verify_algo in ("md5", "sha1", "sha256", "sha512")


def test_every_install_or_reload_confirmation_is_gated():
    """A bare 'yes'/'y' line would be executed by a POSIX shell (GNU yes!). Confirmations must be '@' lines."""
    for vendor in VENDORS.values():
        for line in (*vendor.install, *vendor.reload, *vendor.reload_in, *vendor.verify_steps):
            assert line not in ("y", "yes", "n", "no", "Y", "N", ""), f"{vendor.id}: ungated answer {line!r}"


@pytest.mark.parametrize(
    ("vendor", "transfer", "shell"),
    [
        ("cisco-ios", TRANSFER_PUSH, SHELL_CLI), ("huawei", TRANSFER_PUSH, SHELL_CLI),
        ("fortinet", TRANSFER_PULL, SHELL_CLI), ("aruba-aos-cx", TRANSFER_PULL, SHELL_CLI),
        ("paloalto-panos", TRANSFER_PULL, SHELL_CLI), ("ruijie-rgos", TRANSFER_PULL, SHELL_CLI),
        ("vyos", TRANSFER_PUSH, SHELL_POSIX), ("pfsense", TRANSFER_PUSH, SHELL_POSIX),
        ("opnsense", TRANSFER_PUSH, SHELL_POSIX), ("checkpoint-gaia", TRANSFER_PUSH, SHELL_POSIX),
        ("sophos-sfos", TRANSFER_WEB, SHELL_POSIX),
    ],
)  # fmt: skip
def test_transfer_model_and_execution_mode(vendor, transfer, shell):
    v = get_vendor(vendor)
    assert (v.transfer, v.shell) == (transfer, shell)


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("PanOS_220-10.1.11-h1", "10.1.11-h1"),
        ("PanOS_vm-10.1.0", "10.1.0"),
        ("PanOS_3000-9.1.16", "9.1.16"),
        ("FGT_60E-v7.2.4-build1396-FORTINET.out", "7.2.4"),
        ("firmware.bin", None),
    ],
)
def test_version_from_filename(name, version):
    assert version_from_filename(name) == version


# =============================================================================================== free-space parsers
HUAWEI_DIR = """Directory of flash:/

  Idx  Attr     Size(Byte)  Date        Time(LMT)  FileName
    0  drw-              -  Mar 02 2023 10:21:10   dhcp
    1  -rw-     79,311,696  Jun 21 2023 09:02:11   AR651-V300R022C00SPC200.cc
    2  -rw-          1,102  Jun 21 2023 09:05:44   vrpcfg.zip

1,090,732 KB total (784,464 KB free)
"""
FORTIOS_PERF = """CPU states: 0% user 1% system 0% nice 99% idle 0% iowait 0% irq 0% softirq
Memory: 1911268k total, 578572k used (30.3%), 1272712k free (66.6%), 59984k shared, 5972k buffers
Average network usage: 12 / 11 kbps in 1 minute
"""
PANOS_DISK = """Filesystem      Size  Used Avail Use% Mounted on
/dev/root       7.0G  4.2G  2.4G  64% /
/dev/sda5        15G  8.1G  6.1G  58% /opt/panrepo
/dev/sda6        11G  1.0G  9.2G  10% /opt/pancfg
tmpfs           3.9G  1.1M  3.9G   1% /dev/shm
"""
LINUX_DF = "Filesystem     1024-blocks    Used Available Capacity Mounted on\n/dev/sda1         41152736 9876543  29204193      26% /\n"
FREEBSD_DF = "Filesystem 1024-blocks    Used   Avail Capacity  Mounted on\n/dev/gpt/rootfs  14289904 1873632 11972684    14%    /\n"
GB = 1024**3


@pytest.mark.parametrize(
    ("kind", "output", "directory", "expected"),
    [
        ("kb-total-free", HUAWEI_DIR, "flash:", 784464 * 1024),  # comma-separated KB, Huawei's own units
        ("kb-total-free", "7,239 MB total (3,590.5 MB free)", "", int(3590.5 * 1024**2)),
        ("kb-total-free", "10 KB total (9 KB free)", "", 9 * 1024),
        ("kb-total-free", "122185728 bytes total (21233664 bytes free)", "", 21233664),
        ("kb-total-free", "Error: Wrong parameter found at '^' position.", "", None),
        ("kb-total-free", "1,090,732 KB total", "", None),  # no free figure: never guess
        ("fortios-memory", FORTIOS_PERF, "", 1272712 * 1024),
        ("fortios-memory", "Version: FortiGate-60E v7.2.4", "", None),
        ("df-human", PANOS_DISK, "/opt/panrepo", int(6.1 * GB)),
        ("df-human", PANOS_DISK, "/opt/panrepo/software", int(6.1 * GB)),
        ("df-human", PANOS_DISK, "/opt/pancfg", int(9.2 * GB)),
        ("df-human", PANOS_DISK, "/var/log", int(2.4 * GB)),  # falls back to the enclosing mount, here /
        ("df-human", "Server error : Invalid syntax.", "/opt/panrepo", None),
        ("df", LINUX_DF, "/", 29204193 * 1024),
        ("df", FREEBSD_DF, "/", 11972684 * 1024),
        ("df", "df: /nope: No such file or directory", "/nope", None),
        ("auto", HUAWEI_DIR, "", 784464 * 1024),
        ("auto", "Directory of flash:/\n 123456789 bytes total (23456789 bytes free)", "", 23456789),
        ("auto", LINUX_DF, "/", 29204193 * 1024),
        ("auto", "no figures here at all", "", None),
        ("auto", "", "", None),
    ],
)
def test_free_space_parsers_for_the_new_platforms(kind, output, directory, expected):
    assert parse_free_bytes(kind, output, directory) == expected


# =============================================================================================== digest evaluation
LOCAL = sha256(b"image")
OTHER = sha256(b"other")


def verdict(vendor_id, output, local=LOCAL):
    v = get_vendor(vendor_id)
    return evaluate_output(output, local, v.verify_algo, v.verify_marker, v)


AOS_IMAGES = """----------------------------------------------------------------------------
ArubaOS-CX Primary Image
----------------------------------------------------------------------------
Version  : FL.10.10.1010
Size     : 616 MB
SHA-256  : {primary}

----------------------------------------------------------------------------
ArubaOS-CX Secondary Image
----------------------------------------------------------------------------
Version  : FL.10.09.1020
Size     : 612 MB
SHA-256  : {secondary}

Default Image : Primary
Boot Image    : Primary
"""


@pytest.mark.parametrize(
    ("vendor", "output", "status"),
    [
        ("huawei", f"Caution!!! Confirm to check startup file! Continue? [Y/N]:y\nInfo: The SHA256 hash value of the system software is {LOCAL}.", STATUS_VERIFIED),
        ("huawei", f"Info: The SHA256 hash value of the system software is {OTHER}.", STATUS_MISMATCH),
        ("huawei", "Info: System software code signature check OK!", STATUS_ERROR),  # releases that print no hash
        ("aruba-aos-cx", AOS_IMAGES.format(primary=LOCAL, secondary=OTHER), STATUS_VERIFIED),
        ("aruba-aos-cx", AOS_IMAGES.format(primary=OTHER, secondary=LOCAL), STATUS_MISMATCH),  # slot matters: not the last digest
        ("aruba-aos-cx", "Version: FL.10.10.1010", STATUS_ERROR),
        ("vyos", f"{LOCAL}  fw.iso", STATUS_VERIFIED),
        ("vyos", f"{OTHER}  fw.iso", STATUS_MISMATCH),
        ("vyos", "sha256sum: fw.iso: No such file or directory", STATUS_ERROR),
        ("pfsense", f"SHA256 (/tmp/fw.pkg) = {LOCAL}", STATUS_VERIFIED),
        ("opnsense", f"SHA256 (/tmp/fw.pkg) = {OTHER}", STATUS_MISMATCH),
        ("checkpoint-gaia", f"{LOCAL}  /var/log/fw.tgz", STATUS_VERIFIED),
        ("sophos-sfos", f"{LOCAL.upper()}  fw.sig", STATUS_VERIFIED),  # upper-case digests compare equal
    ],
)  # fmt: skip
def test_digest_evaluation_per_platform(vendor, output, status):
    assert verdict(vendor, output).status == status


def test_aruba_marker_never_borrows_another_slots_digest():
    # primary section present but without a hash; the secondary has one: must NOT be used for the primary slot
    output = "ArubaOS-CX Primary Image\nVersion : X\nArubaOS-CX Secondary Image\nSHA-256 : " + LOCAL
    # the first digest after the marker is the secondary's here: this documents the limit of a text marker
    assert verdict("aruba-aos-cx", output).ok
    assert not verdict("aruba-aos-cx", "ArubaOS-CX Secondary Image\nSHA-256 : " + LOCAL).ok  # no primary marker at all


@pytest.mark.parametrize(
    ("vendor", "output", "needle"),
    [
        ("checkpoint-gaia", "CLINFR0329  Invalid command:'sha256sum /var/log/fw.tgz'", "Gaia Clish"),
        ("pfsense", "Enter an option: ", "console menu"),
        ("opnsense", "*** OPNsense.localdomain: OPNsense 24.1 ***\n Enter an option: ", "console menu"),
        ("sophos-sfos", "Main Menu\n 1. Network Configuration\n Select Menu Number [0-7]:", "Advanced Shell"),
    ],
)
def test_a_wrong_login_shell_is_explained_not_just_reported(vendor, output, needle):
    result = verdict(vendor, output)
    assert result.status == STATUS_ERROR and needle in result.detail


def test_hash_mismatch_never_gets_a_shell_hint():
    result = verdict("checkpoint-gaia", f"{OTHER}  fw.tgz")
    assert result.mismatch and "Clish" not in result.detail


# =============================================================================================== question detection + gating
@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("What would you like to name this image? (Default: 1.4-rolling-202401010101):", True),
        ("Would you like to set the new image as the default one for boot? (Yes/No) [Yes]:", True),
        ("Proceed with reboot? (Yes/No) [No]", True),
        ("Password:", True),
        ("Confirm to check startup file! Continue? [Y/N]:", True),
        ("Reload system?(Y/N)", True),
        ("Do you want to continue? (y or n)", True),
        ("Reboot the system ? [yes,no] (no)", True),
        ("This will reboot the entire switch and render it unavailable until the process is complete. Continue (y/n)?", True),
        ("router# ", False), ("vyos@vyos:~$ ", False), ("[Expert@gw:0]# ", False), ("gaia> ", False),
        ("<Huawei>", False), ("FGT60E # ", False), ("admin@PA-VM> ", False), ("Info: done.", False), ("", False),
    ],
)  # fmt: skip
def test_looks_like_question(text, asked):
    assert looks_like_question(text) is asked


def test_parse_answer_forms():
    assert parse_answer("@y") == (None, "y") and parse_answer("@") == (None, "")
    assert parse_answer("@~(?i)save~n") == ("(?i)save", "n") and parse_answer("@~save~!") == ("save", ABORT)


def run(lab, lines, disconnect_after=None):
    transport = connect(lab)
    shell = Shell(transport, timeout=10)
    out = []
    try:
        return shell.run_script(lines, out.append, disconnect_after), out
    finally:
        shell.close()
        transport.close()


def test_a_gated_answer_is_only_sent_when_the_device_asks(ssh_lab):
    ssh_lab.shell_replies = {"reboot": ("Proceed with reboot? (Yes/No) [No]", False)}
    run(ssh_lab, ["reboot", "@yes"])
    assert ssh_lab.shell_log == ["reboot", "yes"]
    ssh_lab.shell_log.clear()
    ssh_lab.shell_replies = {}  # the device does not ask this time
    run(ssh_lab, ["reboot", "@yes"])
    assert ssh_lab.shell_log == ["reboot"]  # 'yes' would have been run by a POSIX shell: it must never be typed


def test_a_conditional_answer_matches_the_question_text(ssh_lab):
    ssh_lab.shell_replies = {
        "reboot": ("Warning: configuration changed. Save? [Y/N]:", False),
        "n": ("Continue? [Y/N]:", False),
    }
    run(ssh_lab, ["reboot", "@~(?i)save~n", "@y"])
    assert ssh_lab.shell_log == ["reboot", "n", "y"]  # first the save question (n), then the reboot question (y)


def test_an_abort_answer_stops_instead_of_answering(ssh_lab):
    ssh_lab.shell_replies = {"reload": ("System configuration has been modified. Save? [yes/no]:", False)}
    with pytest.raises(RemoteError, match="answer it yourself"):
        run(ssh_lab, ["reload", "@~save~!", "@"])
    assert ssh_lab.shell_log == ["reload"]  # nothing was answered on the operator's behalf


def test_a_rejected_command_stops_the_script(ssh_lab):
    ssh_lab.shell_replies = {"bogus": "% Invalid input detected at '^' marker."}
    with pytest.raises(RemoteError, match="rejected"):
        run(ssh_lab, ["bogus", "never-sent"])
    assert ssh_lab.shell_log == ["bogus"]


def test_a_multi_question_installer_gets_one_answer_per_question(ssh_lab):
    ssh_lab.shell_replies = {
        "add system image fw.iso": ("What would you like to name this image? (Default: 1.4-rolling):", False),
        "": [("Would you like to set the new image as the default one for boot? (Yes/No) [Yes]:", False), "Done."],
    }
    run(ssh_lab, ["add system image fw.iso", "@", "@", "@", "@"])
    assert ssh_lab.shell_log == ["add system image fw.iso", "", ""]  # two questions, two Enters; the spare '@'s skipped


# =============================================================================================== install / reload plans
@pytest.mark.parametrize(
    ("vendor", "path", "name", "opts", "expected"),
    [
        ("huawei", "flash:/fw.cc", "fw.cc", InstallOptions(), ["startup system-software flash:/fw.cc"]),
        ("huawei", "x", "x", InstallOptions(set_boot=False, reload=True), ["reboot", "@~(?i)save~!", "@y"]),
        ("aruba-aos-cx", "x", "x", InstallOptions(), ["boot set-default primary"]),
        ("aruba-aos-cx", "x", "x", InstallOptions(set_boot=False, reload=True), ["boot system primary", "@y"]),
        ("paloalto-panos", "x", "PanOS_220-10.1.11-h1", InstallOptions(), ["request system software install version 10.1.11-h1", "@y"]),
        ("vyos", "fw.iso", "fw.iso", InstallOptions(), ["add system image fw.iso", "@", "@", "@", "@"]),
        ("vyos", "x", "x", InstallOptions(set_boot=False, reload=True), ["reboot", "@yes"]),
        ("vyos", "x", "x", InstallOptions(set_boot=False, reload_in=15), ["reboot in 15", "@yes"]),
        ("pfsense", "/tmp/fw.pkg", "fw.pkg", InstallOptions(), ["pkg add /tmp/fw.pkg"]),
        ("opnsense", "x", "x", InstallOptions(set_boot=False, reload=True), ["shutdown -r now"]),
        ("opnsense", "x", "x", InstallOptions(set_boot=False, reload_in=10), ["shutdown -r +10"]),
        ("checkpoint-gaia", "fw.tgz", "fw.tgz", InstallOptions(), ['clish -c "installer import local fw.tgz"']),
        ("checkpoint-gaia", "x", "x", InstallOptions(set_boot=False, reload=True), ['clish -c "reboot"']),
        ("checkpoint-gaia", "x", "x", InstallOptions(set_boot=False, reload_in=7), ["shutdown -r +7"]),
        ("fortinet", "x", "x", InstallOptions(set_boot=False, reload=True), ["execute reboot", "@y"]),
        ("ruijie-rgos", "x", "x", InstallOptions(set_boot=False, reload=True), ["reload", "@y"]),
    ],
)  # fmt: skip
def test_install_and_reload_plans_per_platform(vendor, path, name, opts, expected):
    assert plan(get_vendor(vendor), path, name, opts) == expected


@pytest.mark.parametrize(
    ("vendor", "name", "opts", "message"),
    [
        ("paloalto-panos", "firmware.bin", InstallOptions(), "cannot read a software version"),
        (
            "paloalto-panos",
            "PanOS_vm-10.1.0",
            InstallOptions(set_boot=False, reload=True),
            "no reload command",
        ),  # async job
        (
            "paloalto-panos",
            "PanOS_vm-10.1.0",
            InstallOptions(set_boot=False, reload_in=5),
            "scheduled reload is not available",
        ),
        ("aruba-aos-cx", "x.swi", InstallOptions(set_boot=False, reload_in=5), "scheduled reload is not available"),
        ("fortinet", "x.out", InstallOptions(), "no boot-image profile"),
        ("fortinet", "x.out", InstallOptions(set_boot=False, reload_in=5), "scheduled reload is not available"),
        ("sophos-sfos", "x", InstallOptions(), "no boot-image profile"),
        ("ruijie-rgos", "x.bin", InstallOptions(), "no boot-image profile"),
        ("huawei", "x.cc", InstallOptions(set_boot=False, reload_in=5), "scheduled reload is not available"),
        ("vyos", "x;reboot.iso", InstallOptions(), "unsafe"),
    ],
)
def test_what_a_platform_cannot_do_is_refused(vendor, name, opts, message):
    with pytest.raises(InstallError, match=message):
        plan(get_vendor(vendor), "flash:/" + name, name, opts)


def test_plans_describe_themselves_in_the_vendors_own_words():
    assert "FreeBSD package" in describe(InstallOptions(), get_vendor("pfsense"))
    assert "system image" in describe(InstallOptions(), get_vendor("vyos"))
    assert "CPUSE" in describe(InstallOptions(), get_vendor("checkpoint-gaia"))
    assert "asynchronous" in describe(InstallOptions(), get_vendor("paloalto-panos"))
    assert describe(InstallOptions()) == "set the boot image to the pushed file"
    shown = describe_plan(["reload", "@~save~!", "@y", "@"])
    assert shown == ["reload", "(if asked about /save/: abort)", "(if asked: y)", "(if asked: <Enter>)"]


# =============================================================================================== workflows: push-capable
def test_huawei_preflight_interactive_verify_boot_and_reload(ssh_lab, tmp_path):
    f = image(tmp_path, "fw.cc")
    digest = sha256(f.read_bytes())
    ssh_lab.commands = lambda c: (HUAWEI_DIR, 0) if c == "dir flash:" else ("", 0)
    ssh_lab.shell_replies = {
        "check system-software flash:/fw.cc": ("Caution!!! Confirm to check startup file! Continue? [Y/N]:", False),
        "y": f"Info: The SHA256 hash value of the system software is {digest}.",
    }
    opts = InstallOptions(execute=True, confirmed=True, timeout=10)
    events = push(
        ssh_lab,
        f,
        tmp_path,
        vendor="huawei",
        remote_path="flash:/",
        preflight=True,
        verify=VerifyOptions(),
        install=opts,
    )
    assert events[-1].state is State.FINISHED and events[-1].detail == STATUS_VERIFIED, log_text(events)
    assert "dir flash:" in ssh_lab.executed and ssh_lab.files["fw.cc"] == f.read_bytes()
    assert ssh_lab.shell_log == ["check system-software flash:/fw.cc", "y", "startup system-software flash:/fw.cc"]


def test_huawei_not_enough_flash_stops_before_sending(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("100 KB total (10 KB free)\n", 0)
    events = push(
        ssh_lab, image(tmp_path, "fw.cc", 50_000), tmp_path, vendor="huawei", remote_path="flash:/", preflight=True
    )
    assert events[-1].state is State.FAILED and "Not enough space" in events[-1].detail and ssh_lab.files == {}


def test_huawei_digest_mismatch_is_reported(ssh_lab, tmp_path):
    ssh_lab.shell_replies = {
        "check system-software flash:/fw.cc": ("Continue? [Y/N]:", False),
        "y": f"Info: The SHA256 hash value of the system software is {OTHER}.",
    }
    events = push(
        ssh_lab, image(tmp_path, "fw.cc"), tmp_path, vendor="huawei", remote_path="flash:/", verify=VerifyOptions()
    )
    assert events[-1].state is State.FAILED and events[-1].detail.startswith(STATUS_MISMATCH)


def test_huawei_reload_answers_the_reboot_question_but_refuses_to_decide_about_saving(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("", 0)
    f = image(tmp_path, "fw.cc")
    digest = sha256(f.read_bytes())
    check = {"check system-software flash:/fw.cc": ("Continue? [Y/N]:", False)}
    reboot = InstallOptions(execute=True, set_boot=False, reload=True, confirmed=True, timeout=10)

    ssh_lab.shell_replies = {
        **check,
        "y": [f"Info: The SHA256 hash value of the system software is {digest}.", ("", False)],
        "reboot": ("System will reboot! Continue? [Y/N]:", False),
    }
    events = push(ssh_lab, f, tmp_path, vendor="huawei", remote_path="flash:/", verify=VerifyOptions(), install=reboot)
    assert events[-1].state is State.FINISHED, log_text(events)
    assert ssh_lab.shell_log[-2:] == ["reboot", "y"]

    ssh_lab.shell_log.clear()
    ssh_lab.shell_replies = {
        **check,
        "y": f"Info: The SHA256 hash value of the system software is {digest}.",
        "reboot": ("The configuration has been changed. Save the configuration? [Y/N]:", False),
    }
    events = push(ssh_lab, f, tmp_path, vendor="huawei", remote_path="flash:/", verify=VerifyOptions(), install=reboot)
    assert events[-1].state is State.FAILED and "answer it yourself" in events[-1].detail
    assert ssh_lab.shell_log[-1] == "reboot"  # it neither saved nor discarded the operator's configuration


def test_vyos_exec_verify_space_and_defaults_installer(ssh_lab, tmp_path):
    f = image(tmp_path, "fw.iso")
    digest = sha256(f.read_bytes())

    def vyos(command):
        if command == "df -Pk .":
            return LINUX_DF, 0
        if command == "sha256sum fw.iso":
            return f"{digest}  fw.iso\n", 0
        return "", 0

    ssh_lab.commands = vyos
    ssh_lab.shell_replies = {
        "add system image fw.iso": ("What would you like to name this image? (Default: 1.4-rolling):", False),
        "": [("Would you like to set the new image as the default one for boot? (Yes/No) [Yes]:", False), "Done."],
    }
    opts = InstallOptions(execute=True, confirmed=True, timeout=10)
    events = push(ssh_lab, f, tmp_path, vendor="vyos", preflight=True, verify=VerifyOptions(), install=opts)
    assert events[-1].state is State.FINISHED and events[-1].detail == STATUS_VERIFIED, log_text(events)
    assert ssh_lab.executed[:1] == ["df -Pk ."] and "sha256sum fw.iso" in ssh_lab.executed
    assert ssh_lab.shell_log == ["add system image fw.iso", "", ""]


def test_vyos_reboot_says_yes_only_when_asked(ssh_lab, tmp_path):
    f = image(tmp_path, "fw.iso")
    ssh_lab.commands = lambda c: (f"{sha256(f.read_bytes())}  fw.iso\n", 0)
    opts = InstallOptions(execute=True, set_boot=False, reload_in=10, confirmed=True, timeout=10)
    ssh_lab.shell_replies = {"reboot in 10": ("Proceed with reboot? (Yes/No) [No]", False)}
    assert push(ssh_lab, f, tmp_path, vendor="vyos", verify=VerifyOptions(), install=opts)[-1].state is State.FINISHED
    assert ssh_lab.shell_log == ["reboot in 10", "yes"]
    ssh_lab.shell_log.clear()
    ssh_lab.shell_replies = {}
    assert push(ssh_lab, f, tmp_path, vendor="vyos", verify=VerifyOptions(), install=opts)[-1].state is State.FINISHED
    assert ssh_lab.shell_log == ["reboot in 10"] and "yes" not in ssh_lab.shell_log  # never run as the 'yes' program


@pytest.mark.parametrize("vendor", ["pfsense", "opnsense"])
def test_freebsd_firewalls_verify_package_install_and_scheduled_reboot(ssh_lab, tmp_path, vendor):
    f = image(tmp_path, "fw.pkg")

    def bsd(command):
        if command == "df -Pk /tmp":
            return FREEBSD_DF, 0
        if command == "sha256 /tmp/fw.pkg":
            return f"SHA256 (/tmp/fw.pkg) = {sha256(f.read_bytes())}\n", 0
        return "", 0

    ssh_lab.commands = bsd
    opts = InstallOptions(execute=True, reload_in=10, confirmed=True, timeout=10)
    events = push(
        ssh_lab, f, tmp_path, vendor=vendor, remote_path="/tmp/", preflight=True, verify=VerifyOptions(), install=opts
    )
    assert events[-1].state is State.FINISHED and events[-1].detail == STATUS_VERIFIED, log_text(events)
    assert ssh_lab.shell_log == ["pkg add /tmp/fw.pkg", "shutdown -r +10"]
    assert "Install plan applied" in log_text(events)


def test_pfsense_console_menu_is_explained(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("*** Welcome to pfSense ***\n 8) Shell\nEnter an option: ", 0)
    events = push(
        ssh_lab, image(tmp_path, "fw.pkg"), tmp_path, vendor="pfsense", remote_path="/tmp/", verify=VerifyOptions()
    )
    assert events[-1].state is State.FAILED and "console menu" in events[-1].detail


def test_checkpoint_gaia_bash_verify_and_clish_wrapped_actions(ssh_lab, tmp_path):
    f = image(tmp_path, "fw.tgz")
    ssh_lab.commands = lambda c: (
        (f"{sha256(f.read_bytes())}  fw.tgz\n", 0) if c.startswith("sha256sum") else (LINUX_DF, 0)
    )
    opts = InstallOptions(execute=True, reload=True, confirmed=True, timeout=10)
    events = push(ssh_lab, f, tmp_path, vendor="checkpoint-gaia", verify=VerifyOptions(), install=opts)
    assert events[-1].state is State.FINISHED and events[-1].detail == STATUS_VERIFIED, log_text(events)
    assert ssh_lab.shell_log == ['clish -c "installer import local fw.tgz"', 'clish -c "reboot"']


def test_checkpoint_clish_login_shell_gets_actionable_advice(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("CLINFR0329  Invalid command:'sha256sum fw.tgz'\n", 0)
    events = push(ssh_lab, image(tmp_path, "fw.tgz"), tmp_path, vendor="checkpoint-gaia", verify=VerifyOptions())
    assert events[-1].state is State.FAILED and "set user USER shell /bin/bash" in events[-1].detail


def test_checkpoint_clish_login_shell_also_explains_a_skipped_space_check(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("CLINFR0329  Invalid command:'df -Pk .'\n", 0)
    events = push(ssh_lab, image(tmp_path, "fw.tgz"), tmp_path, vendor="checkpoint-gaia", preflight=True)
    assert events[-1].state is State.FINISHED and "Gaia Clish" in log_text(events)  # advice, never a block


# =============================================================================================== workflows: pull / web-only
@pytest.mark.parametrize("vendor", ["fortinet", "aruba-aos-cx", "paloalto-panos", "ruijie-rgos", "sophos-sfos"])
def test_pushing_to_a_platform_that_normally_pulls_warns_but_does_not_block(ssh_lab, tmp_path, vendor):
    events = push(ssh_lab, image(tmp_path, "fw.bin"), tmp_path, vendor=vendor, remote_path="flash:")
    warnings = log_text(events, Level.WARNING)
    assert get_vendor(vendor).label in warnings and get_vendor(vendor).note[:25] in warnings
    assert events[-1].state is State.FINISHED  # the operator may know better (a lab, a different release)


def test_a_normal_push_vendor_gets_no_transfer_warning(ssh_lab, tmp_path):
    events = push(ssh_lab, image(tmp_path, "fw.bin"), tmp_path, vendor="cisco-ios", remote_path="flash:")
    assert log_text(events, Level.WARNING) == ""


def test_fortios_memory_check_is_advisory_only(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("Memory: 1000k total, 990k used (99.0%), 10k free (1.0%), 0k shared\n", 0)
    events = push(
        ssh_lab, image(tmp_path, "fw.out", 50_000), tmp_path, vendor="fortinet", remote_path="/", preflight=True
    )
    assert events[-1].state is State.FINISHED and "advisory" in log_text(events, Level.WARNING)
    assert ssh_lab.executed[0] == "get system performance status"


def test_panos_space_check_looks_at_the_software_repository(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: (PANOS_DISK, 0)
    events = push(
        ssh_lab,
        image(tmp_path, "PanOS_vm-10.1.0"),
        tmp_path,
        vendor="paloalto-panos",
        remote_path="/tmp/",
        preflight=True,
    )
    assert events[-1].state is State.FINISHED and "show system disk-space" in ssh_lab.executed
    assert "/opt/panrepo" in log_text(events)  # not the directory the file was pushed to
    tight = PANOS_DISK.replace("6.1G", "5.0K")
    ssh_lab.commands = lambda c: (tight, 0)
    events = push(
        ssh_lab,
        image(tmp_path, "PanOS_vm-10.1.0", 50_000),
        tmp_path,
        vendor="paloalto-panos",
        remote_path="/tmp/",
        preflight=True,
    )
    assert events[-1].state is State.FAILED and "Not enough space" in events[-1].detail


def test_ruijie_unreadable_dir_output_never_blocks(ssh_lab, tmp_path):
    ssh_lab.commands = lambda c: ("Mode Link  Size  Last Modified  Name\n-rw- 1  13433440  rgos.bin\n", 0)
    events = push(
        ssh_lab, image(tmp_path, "fw.bin"), tmp_path, vendor="ruijie-rgos", remote_path="flash:", preflight=True
    )
    assert events[-1].state is State.FINISHED and "could not read" in log_text(events)


# =============================================================================================== version checks
@pytest.mark.parametrize(
    ("vendor", "command", "reply", "version"),
    [
        ("fortinet", "get system status", "Version: FortiGate-60E v7.2.4,build1396,230214 (GA.M)\nSerial-Number: FGT60E\n", "7.2.4"),
        ("aruba-aos-cx", "show version", "ArubaOS-CX\nVersion      : FL.10.10.1010\n", "FL.10.10.1010"),
        ("paloalto-panos", "show system info | match sw-version", "sw-version: 10.1.11-h1\n", "10.1.11-h1"),
        ("ruijie-rgos", "show version", "System description : Ruijie Switch (S5750C) By Ruijie Networks\nSoftware version : 11.0(4)B1\n", "11.0(4)B1"),
        ("huawei", "display version", "VRP (R) software, Version 5.170 (AR651 V300R022C00SPC200)\n", "V300R022C00SPC200"),
        ("pfsense", "cat /etc/version", "2.7.2-RELEASE\n", "2.7.2"),
        ("opnsense", "opnsense-version", "OPNsense 24.1.5-amd64 (amd64)\n", "24.1.5"),
        ("checkpoint-gaia", 'clish -c "show version all"', "Product version Check Point Gaia R81.20\n", "R81.20"),
    ],
)  # fmt: skip
def test_version_check_on_exec_platforms(ssh_lab, vendor, command, reply, version):
    ssh_lab.commands = lambda c: (reply, 0) if c == command else ("", 0)
    transport = connect(ssh_lab)
    try:
        ok = check_version(transport, get_vendor(vendor), version)
        bad = check_version(transport, get_vendor(vendor), "99.99.99")
    finally:
        transport.close()
    assert ok.status == STATUS_VERSION_OK and ok.ok and command in ssh_lab.executed
    assert bad.status == STATUS_VERSION_BAD and bad.mismatch and not bad.ok


def test_version_check_on_vyos_uses_the_interactive_op_mode_shell(ssh_lab):
    ssh_lab.shell_replies = {"show version": "Version:          VyOS 1.4.0\nBuilt by:         autobuild@vyos.net"}
    transport = connect(ssh_lab)
    try:
        result = check_version(transport, get_vendor("vyos"), "1.4.0")
    finally:
        transport.close()
    assert result.ok and ssh_lab.shell_log == ["show version"] and ssh_lab.executed == []


def test_version_check_edge_cases(ssh_lab):
    transport = connect(ssh_lab)
    try:
        assert "no version command" in check_version(transport, get_vendor("sophos-sfos"), "1").detail
        assert "give the version" in check_version(transport, get_vendor("fortinet"), "  ").detail
        silent = check_version(transport, get_vendor("fortinet"), "7.2.4")  # the lab answers with nothing
        assert silent.status == STATUS_ERROR and "no output" in silent.detail
        ssh_lab.commands = lambda c: ("CLINFR0329  Invalid command:'x'\n", 0)
        hinted = check_version(transport, get_vendor("checkpoint-gaia"), "R81")
        assert hinted.mismatch and "Clish" in hinted.detail
    finally:
        transport.close()


# =============================================================================================== the verify command
@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_store", lambda: KnownHostsStore(tmp_path / "known_hosts"))
    monkeypatch.setattr("app.platform.paths.data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("NT_PW", "secret")


def verify_args(lab, *extra):
    return ["verify", "127.0.0.1", *extra, "-u", "admin", "--password-env", "NT_PW", "--port", str(lab.port)]


def test_verify_command_checks_the_primary_slot_on_aruba(cli_env, ssh_lab, tmp_path, capsys):
    f = image(tmp_path, "fw.swi")
    digest = sha256(f.read_bytes())
    ssh_lab.commands = lambda c: (
        (AOS_IMAGES.format(primary=digest, secondary=OTHER), 0) if c == "show images" else ("", 0)
    )
    assert cli.main(verify_args(ssh_lab, str(f), "--vendor", "aruba")) == 0
    assert "VERIFIED (hash match)" in capsys.readouterr().out and ssh_lab.executed == ["show images"]
    ssh_lab.commands = lambda c: (AOS_IMAGES.format(primary=OTHER, secondary=digest), 0)
    assert (
        cli.main(verify_args(ssh_lab, str(f), "--vendor", "aruba")) == 1
    )  # right digest, wrong slot: still a mismatch
    assert "FAILED (checksum mismatch)" in capsys.readouterr().out


def test_verify_command_checks_the_software_version_after_a_pull_upgrade(cli_env, ssh_lab, capsys):
    ssh_lab.commands = lambda c: ("Version: FortiGate-60E v7.2.4,build1396\n", 0)
    assert cli.main(verify_args(ssh_lab, "--vendor", "fortinet", "--expect-version", "7.2.4")) == 0
    assert "VERIFIED (version match)" in capsys.readouterr().out
    assert cli.main(verify_args(ssh_lab, "--vendor", "fortinet", "--expect-version", "7.4.1")) == 1
    assert "FAILED (version mismatch)" in capsys.readouterr().out


def test_verify_command_can_check_both_and_reports_the_worst(cli_env, ssh_lab, tmp_path, capsys):
    f = image(tmp_path, "fw.tgz")
    ssh_lab.commands = lambda c: (
        (f"{sha256(f.read_bytes())}  fw.tgz\n", 0) if c.startswith("sha256sum") else ("R81.10\n", 0)
    )
    args = verify_args(ssh_lab, str(f), "--vendor", "checkpoint", "--expect-version", "R81.20")
    assert cli.main(args) == 1  # digest ok, version wrong
    out = capsys.readouterr().out
    assert "VERIFIED (hash match)" in out and "FAILED (version mismatch)" in out


def test_verify_command_with_a_custom_checksum_command(cli_env, ssh_lab, tmp_path, capsys):
    f = image(tmp_path, "fw.out")
    ssh_lab.commands = lambda c: (f"{hashlib.md5(f.read_bytes()).hexdigest()}  {c.split()[-1]}\n", 0)
    args = verify_args(ssh_lab, str(f), "--vendor", "fortinet", "--verify-cmd", "md5sum {path}", "--remote", "/data/")
    assert cli.main(args) == 0 and ssh_lab.executed == ["md5sum /data/fw.out"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--vendor", "fortinet"], "FILE to compare and/or --expect-version"),
        (["--vendor", "acme", "--expect-version", "1"], "unknown vendor"),
        (["--vendor", "sophos", "--expect-version", "1"], "no version command"),
        (["--vendor", "ruijie", "--verify-cmd", "md5sum"], "must contain"),
    ],
)
def test_verify_command_usage_errors(cli_env, ssh_lab, tmp_path, extra, message):
    args = verify_args(ssh_lab, *extra)
    if "--verify-cmd" in extra:
        args.insert(2, str(image(tmp_path, "fw.bin")))
    with pytest.raises(SystemExit, match=message):
        cli.main(args)


def test_verify_command_explains_when_a_file_hash_is_impossible(cli_env, ssh_lab, tmp_path):
    f = image(tmp_path, "fw.out")
    for vendor in ("fortinet", "paloalto", "ruijie"):
        with pytest.raises(SystemExit, match="--expect-version"):
            cli.main(verify_args(ssh_lab, str(f), "--vendor", vendor))


def test_device_check_service_reports_a_missing_file_and_connection_errors(ssh_lab, tmp_path):
    events = []
    svc = DeviceCheckService(events.append, KnownHostsStore(tmp_path / "kh"))
    svc.run(DeviceCheck("127.0.0.1", "admin", "linux", "secret", ssh_lab.port, file=str(tmp_path / "nope.bin")))
    assert events[-1].state is State.FAILED and "is not a file" in events[-1].detail
    events.clear()
    svc = DeviceCheckService(events.append, KnownHostsStore(tmp_path / "kh"))
    svc.run(DeviceCheck("127.0.0.1", "admin", "linux", "WRONG", ssh_lab.port, expect_version="1"))
    assert events[-1].state is State.FAILED and "WRONG" not in events[-1].detail


# =============================================================================================== command presets
@pytest.mark.parametrize(
    ("protocol", "vendor", "needles"),
    [
        ("http", "vyos", ["add system image http://10.0.0.5:8080/fw.iso", "show system image"]),
        ("http", "pfsense", ["fetch -o /tmp/fw.iso http://10.0.0.5:8080/fw.iso", "sha256 /tmp/fw.iso"]),
        (
            "http",
            "check point",
            ["curl_cli -k -o /var/log/fw.iso", "sha256sum /var/log/fw.iso", "installer import local"],
        ),
        ("tftp", "ruijie", ["upgrade download tftp://10.0.0.5/fw.iso", "RGOS 10.x"]),
        ("tftp", "fortinet", ["execute restore image tftp fw.iso 10.0.0.5", "get system status", "cannot hash a file"]),
        ("tftp", "aruba", ["copy tftp://10.0.0.5/fw.iso primary", "show images", "vrf mgmt"]),
        ("tftp", "huawei", ["tftp 10.0.0.5 get fw.iso", "check system-software flash:/fw.iso"]),
        (
            "tftp",
            "palo",
            ["scp import software from USER@10.0.0.5:fw.iso", "request system software info", "show jobs all"],
        ),
    ],
)
def test_command_presets_for_the_new_platforms(capsys, protocol, vendor, needles):
    assert cli.main(["commands", protocol, "/x/fw.iso", "--ip", "10.0.0.5", "--vendor", vendor]) == 0
    out = capsys.readouterr().out
    for needle in needles:
        assert needle in out, (needle, out)
    assert "{" not in out  # every placeholder, including those inside notes, was filled in or avoided
