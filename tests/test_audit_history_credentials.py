"""Hash-chained audit log, history parsing/report, and keyring-backed credentials."""

import os

from app.core.audit import GENESIS, AuditLog, chain_tag, last_tag, load_or_create_key, verify_chain
from app.core.batch import BatchTarget
from app.core.credentials import CredentialStore, KeyringUnavailableError
from app.core.events import Level, LogEvent
from app.core.history import load_history, parse_lines, render_html, text_summary
from app.core.requests import PushDefaults


def ev(msg, level=Level.INFO):
    return LogEvent(level, msg, ts=1_700_000_000.0)


# ------------------------------------------------------------------ audit chain
def test_chain_verifies_and_detects_edits_deletions_and_insertions(tmp_path):
    audit = AuditLog(tmp_path)
    for n in range(5):
        audit.write("Push", ev(f"line {n}"))
    path = audit.path
    ok = verify_chain(path)
    assert ok.ok and ok.chained == 5 and ok.legacy == 0 and ok.head == last_tag(path)

    lines = path.read_text().splitlines()
    edited = list(lines)
    edited[2] = edited[2].replace("line 2", "line X")
    path.write_text("\n".join(edited) + "\n")
    report = verify_chain(path)
    assert not report.ok and "line 3" in report.problem

    path.write_text("\n".join([*lines[:2], *lines[3:]]) + "\n")  # delete a middle line
    assert not verify_chain(path).ok

    path.write_text("\n".join([*lines[:3], "2024-01-01 00:00:00 [Push] INFO    forged", *lines[3:]]) + "\n")
    assert "no chain tag" in verify_chain(path).problem


def test_chain_continues_across_instances_and_restarts(tmp_path):
    AuditLog(tmp_path).write("A", ev("one"))
    AuditLog(tmp_path).write("A", ev("two"))  # a new process picks the chain up from the file
    assert verify_chain(AuditLog(tmp_path).path).ok


def test_legacy_lines_before_the_chain_are_tolerated_but_reported(tmp_path):
    audit = AuditLog(tmp_path)
    audit.path.parent.mkdir(parents=True, exist_ok=True)
    audit.path.write_text("2024-01-01 00:00:00 [Push] INFO    old format\n")
    audit.write("Push", ev("new"))
    report = verify_chain(audit.path)
    assert report.ok and report.legacy == 1 and report.chained == 1


def test_hmac_key_changes_the_tags_and_is_required_to_verify(tmp_path):
    key = os.urandom(32)
    audit = AuditLog(tmp_path, key=key)
    audit.write("Push", ev("secret-keyed"))
    assert verify_chain(audit.path, key).ok
    assert not verify_chain(audit.path).ok  # plain SHA-256 cannot reproduce an HMAC chain
    assert chain_tag(GENESIS, "x", key) != chain_tag(GENESIS, "x")


def test_key_file_is_created_once_with_private_permissions(tmp_path):
    path = tmp_path / "data" / "audit.key"
    key = load_or_create_key(path)
    assert key and len(key) == 32 and load_or_create_key(path) == key
    if os.name == "posix":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_audit_lines_still_contain_the_message(tmp_path):
    audit = AuditLog(tmp_path)
    audit.write("TFTP", ev("multi\nline"))
    assert "multi line" in audit.path.read_text()


# ------------------------------------------------------------------ history
LOG = """\
2026-10-01 09:00:00 [Push] INFO    Connecting to admin@10.1.0.1:22 (SFTP) ...
2026-10-01 09:00:30 [Push] SUCCESS Pushed fw-1.bin to 10.1.0.1 via SFTP in 29.0s (4.0 MB/s avg)
2026-10-01 09:01:10 [Push] SUCCESS Verified fw-1.bin on 10.1.0.1: MD5 {md5} matches on the device
2026-10-01 09:01:12 [Push] SUCCESS Install plan applied on 10.1.0.1
2026-10-02 10:00:00 [Batch Push] SUCCESS [10.1.0.2] Pushed fw-2.bin to 10.1.0.2 via SCP in 5.0s (9.0 MB/s avg)
2026-10-02 10:00:05 [Batch Push] ERROR   [10.1.0.3] Authentication failed for 'admin'.
2026-10-02 11:00:00 [TFTP] INFO    10.9.0.5: requested 'boot.img' -> serving fw-3.bin (1.0 MB)
2026-10-02 11:00:09 [TFTP] SUCCESS 10.9.0.5: sent 1.0 MB (1.2 MB/s avg)
2026-10-02 12:00:00 [HTTP] INFO    10.9.0.6: GET /x -> serving fw-4.bin (2.0 MB)
2026-10-02 12:00:03 [HTTP] SUCCESS 10.9.0.6: sent 2.0 MB (3.0 MB/s avg)
""".format(md5="a" * 32)


def test_parse_lines_extracts_pushes_servers_verification_and_errors():
    h = parse_lines(LOG.splitlines())
    by_dev = {r.device: r for r in h.records}
    assert by_dev["10.1.0.1"].file == "fw-1.bin" and by_dev["10.1.0.1"].verified.startswith("MD5 aaaa")
    assert by_dev["10.1.0.1"].installed and by_dev["10.1.0.1"].via == "SFTP"
    assert by_dev["10.1.0.2"].via == "SCP" and not by_dev["10.1.0.2"].verified
    assert by_dev["10.9.0.5"].file == "fw-3.bin" and by_dev["10.9.0.5"].via == "TFTP"
    assert by_dev["10.9.0.6"].file == "fw-4.bin" and by_dev["10.9.0.6"].via == "HTTP"
    assert h.errors == [("2026-10-02 10:00:05", "10.1.0.3", "Authentication failed for 'admin'.")]


def test_chain_tags_do_not_confuse_the_parser(tmp_path):
    audit = AuditLog(tmp_path)
    audit.write("Push", LogEvent(Level.SUCCESS, "Pushed a.bin to h1 via SCP in 1.0s (1 MB/s avg)"))
    h = load_history(tmp_path)
    assert [(r.device, r.file) for r in h.records] == [("h1", "a.bin")]


def test_latest_per_device_filter_and_summary():
    later = "2026-10-03 08:00:00 [Push] SUCCESS Pushed fw-9.bin to 10.1.0.1 via SCP in 1.0s (1 MB/s avg)"
    h = parse_lines([*LOG.splitlines(), later])
    assert h.latest_per_device()["10.1.0.1"].file == "fw-9.bin"
    assert [r.device for r in h.filtered(device="10.9").records] == ["10.9.0.5", "10.9.0.6"]
    assert [r.file for r in h.filtered(file="fw-2").records] == ["fw-2.bin"]
    assert "10.1.0.1" in text_summary(h)


def test_html_report_is_self_contained_and_escaped():
    h = parse_lines(
        ["2026-10-03 08:00:00 [Push] SUCCESS Pushed <script>alert(1)</script>.bin to h1 via SCP in 1.0s (1 MB/s avg)"]
    )
    page = render_html(h)
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "default-src 'none'" in page and "http://" not in page and "https://" not in page


# ------------------------------------------------------------------ credentials
class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, password):
        self.data[(service, account)] = password

    def delete_password(self, service, account):
        if (service, account) not in self.data:
            raise KeyError(account)
        del self.data[(service, account)]


def test_credential_store_exact_then_wildcard_and_delete():
    store = CredentialStore(FakeKeyring())
    store.set("*", "admin", "fallback")
    store.set("10.0.0.1", "admin", "specific")
    assert store.get("10.0.0.1", "admin") == "specific"
    assert store.get("10.0.0.2", "admin") == "fallback"
    assert store.get("10.0.0.2", "other") is None
    assert store.delete("10.0.0.1", "admin") and not store.delete("10.0.0.1", "admin")


def test_missing_keyring_degrades_to_none_but_set_explains():
    class Broken:
        def get_password(self, *_a):
            raise RuntimeError("no backend")

        set_password = get_password

    store = CredentialStore(Broken())
    assert store.get("h", "u") is None
    try:
        store.set("h", "u", "p")
    except KeyringUnavailableError as exc:
        assert "refused" in str(exc)
    else:
        raise AssertionError("expected KeyringUnavailableError")


def test_push_defaults_use_the_keyring_only_when_no_password_is_given():
    lookups = []

    def lookup(host, user):
        lookups.append((host, user))
        return "from-keyring"

    defaults = PushDefaults("scp", "admin", credentials=lookup)
    assert defaults.build("10.0.0.1", "f.bin").password == "from-keyring"
    assert PushDefaults("scp", "admin", "typed", credentials=lookup).build("h", "f.bin").password == "typed"
    # a per-device password in the CSV wins; a per-device username is used for the lookup
    assert defaults.build("h", "f.bin", BatchTarget(0, "h", "ops", "csv-pw")).password == "csv-pw"
    lookups.clear()
    defaults.build("h2", "f.bin", BatchTarget(0, "h2", "ops"))
    assert lookups == [("h2", "ops")]
    assert "from-keyring" not in repr(defaults) and "lookup" not in repr(defaults)


def test_vendor_per_device_overrides_the_default():
    defaults = PushDefaults("scp", "u", "p", vendor="cisco-ios")
    assert defaults.build("h", "f").vendor == "cisco-ios"
    assert defaults.build("h", "f", BatchTarget(0, "h", vendor="junos")).vendor == "junos"
