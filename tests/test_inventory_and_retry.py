"""Inventory import/filter, per-device vendor+tags in the CSV, and batch retry with backoff."""

import json
import threading

import pytest

from app.core.batch import BatchFileError, BatchRunner, BatchTarget, load_targets_csv
from app.core.events import BatchEvent, LogEvent, State, StateEvent
from app.core.inventory import filter_targets, load_inventory

ANSIBLE = """
all:
  vars:
    ansible_user: netops
  children:
    dctt:
      hosts:
        sw-dctt-1: {ansible_host: 10.1.0.1, ansible_network_os: cisco.ios.ios}
        fw-dctt-1: {ansible_host: 10.1.0.2, ansible_user: fwadmin, ansible_port: 2222}
    cmt8:
      hosts:
        sw-cmt8-1: {ansible_host: 10.2.0.1}
"""

NETMIKO = """
- {host: 10.9.0.1, username: u1, device_type: juniper_junos, site: SHTP}
- {ip: 10.9.0.2, device_type: cisco_nxos, tags: [lab, edge]}
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_csv_gets_vendor_and_tags_columns(tmp_path):
    path = write(tmp_path, "t.csv", "host,vendor,tags\n10.0.0.1,cisco-ios,site:A role:core\n10.0.0.2,,\n")
    t = load_targets_csv(path)
    assert t[0].vendor == "cisco-ios" and t[0].tags == ("site:A", "role:core") and t[1].tags == ()


def test_ansible_yaml_inventory(tmp_path):
    pytest.importorskip("yaml")
    t = load_inventory(write(tmp_path, "inv.yml", ANSIBLE))
    by_host = {x.host: x for x in t}
    assert set(by_host) == {"10.1.0.1", "10.1.0.2", "10.2.0.1"}
    assert by_host["10.1.0.1"].username == "netops" and by_host["10.1.0.1"].vendor == "cisco-ios"
    assert by_host["10.1.0.2"].username == "fwadmin" and by_host["10.1.0.2"].port == 2222
    assert "group:dctt" in by_host["10.1.0.1"].tags and "name:sw-dctt-1" in by_host["10.1.0.1"].tags


def test_netmiko_inventory_yaml_and_json(tmp_path):
    pytest.importorskip("yaml")
    t = load_inventory(write(tmp_path, "d.yaml", NETMIKO))
    assert [x.host for x in t] == ["10.9.0.1", "10.9.0.2"]
    assert t[0].vendor == "junos" and "site:SHTP" in t[0].tags and "vendor:junos" in t[0].tags
    assert "lab" in t[1].tags and t[1].vendor == "cisco-nxos"
    as_json = write(tmp_path, "d.json", json.dumps([{"host": "h1", "port": "2200"}]))
    assert load_inventory(as_json)[0].port == 2200


def test_zabbix_export(tmp_path):
    export = {
        "zabbix_export": {
            "hosts": [
                {
                    "host": "fw1",
                    "name": "Firewall 1",
                    "groups": [{"name": "Firewalls"}],
                    "tags": [{"tag": "site", "value": "VPTT"}],
                    "interfaces": [{"ip": "10.5.0.1", "useip": "YES", "port": "161"}],
                },
                {"host": "sw1", "interfaces": [{"dns": "sw1.example", "useip": "NO"}]},
            ]
        }
    }
    t = load_inventory(write(tmp_path, "z.json", json.dumps(export)))
    assert t[0].host == "10.5.0.1" and "group:Firewalls" in t[0].tags and "site:VPTT" in t[0].tags
    assert t[1].host == "sw1.example"


def test_bad_inventories_report_clearly(tmp_path):
    with pytest.raises(BatchFileError, match="no host"):
        load_inventory(write(tmp_path, "b.json", json.dumps([{"username": "x"}])))
    with pytest.raises(BatchFileError, match="invalid port"):
        load_inventory(write(tmp_path, "p.json", json.dumps([{"host": "h", "port": 99999}])))
    with pytest.raises(BatchFileError, match="Zabbix"):
        load_inventory(write(tmp_path, "z.json", json.dumps({"a": 1})), "zabbix")
    with pytest.raises(BatchFileError, match="unknown inventory format"):
        load_inventory(write(tmp_path, "z2.json", "[]"), "nope")


def targets():
    return [
        BatchTarget(0, "10.1.0.1", tags=("site:DCTT", "vendor:fortinet", "group:core")),
        BatchTarget(1, "10.1.0.2", tags=("site:DCTT", "vendor:juniper", "group:lab")),
        BatchTarget(2, "10.2.0.1", tags=("site:SHTP", "vendor:fortinet")),
    ]


@pytest.mark.parametrize(
    ("expr", "hosts"),
    [
        ("", ["10.1.0.1", "10.1.0.2", "10.2.0.1"]),
        ("site:DCTT", ["10.1.0.1", "10.1.0.2"]),
        ("site:dctt vendor:fortinet", ["10.1.0.1"]),
        ("vendor:fortinet,juniper site:DCTT", ["10.1.0.1", "10.1.0.2"]),
        ("!group:lab", ["10.1.0.1", "10.2.0.1"]),
        ("host:10.2.*", ["10.2.0.1"]),
        ("10.1.0.2", ["10.1.0.2"]),
        ("site:nowhere", []),
    ],
)
def test_filter_expressions(expr, hosts):
    out = filter_targets(targets(), expr)
    assert [t.host for t in out] == hosts
    assert [t.index for t in out] == list(range(len(out)))  # re-indexed for the runner


# ------------------------------------------------------------------ retry / backoff
class Flaky:
    """Service that fails transiently ``fail_times`` times, then succeeds."""

    attempts = {}

    def __init__(self, sink, fail_times, transient=True):
        self.sink, self.fail_times, self.transient = sink, fail_times, transient

    def push(self, request):
        n = Flaky.attempts[request] = Flaky.attempts.get(request, 0) + 1
        self.sink(StateEvent(State.STARTED, request))
        if n <= self.fail_times:
            self.sink(StateEvent(State.FAILED, "connection reset", self.transient))
        else:
            self.sink(StateEvent(State.FINISHED, request))

    def cancel(self):
        pass


def run(targets_, fail_times, transient=True, retries=2, backoff=0.01):
    Flaky.attempts = {}
    events = []
    runner = BatchRunner(
        events.append,
        lambda sink: Flaky(sink, fail_times, transient),
        lambda t: t.host,
        max_workers=2,
        retries=retries,
        backoff=backoff,
    )
    runner.run(targets_)
    return runner, events


def test_transient_failures_are_retried_until_success():
    runner, events = run([BatchTarget(0, "a")], fail_times=2)
    assert runner.results[0] == ("done", "")
    assert Flaky.attempts["a"] == 3
    assert [e.detail for e in events if isinstance(e, BatchEvent) and e.status == "retrying"] == [
        "retry 1/2 in 0.01s",
        "retry 2/2 in 0.02s",
    ]  # exponential backoff
    assert not any(isinstance(e, BatchEvent) and e.status == "failed" for e in events)  # no premature failure row


def test_retries_are_bounded_and_the_final_failure_is_reported():
    runner, _ = run([BatchTarget(0, "a")], fail_times=99, retries=2)
    assert runner.results[0] == ("failed", "connection reset") and Flaky.attempts["a"] == 3


def test_permanent_failures_are_not_retried():
    runner, _ = run([BatchTarget(0, "a")], fail_times=99, transient=False)
    assert runner.results[0][0] == "failed" and Flaky.attempts["a"] == 1


def test_no_retries_by_default_keeps_old_behaviour():
    runner, _ = run([BatchTarget(0, "a")], fail_times=1, retries=0)
    assert runner.results[0][0] == "failed" and Flaky.attempts["a"] == 1


def test_cancel_during_backoff_stops_waiting():
    Flaky.attempts = {}
    events = []
    runner = BatchRunner(
        events.append, lambda sink: Flaky(sink, 99), lambda t: t.host, retries=3, backoff=30.0, max_backoff=30.0
    )
    thread = threading.Thread(target=runner.run, args=([BatchTarget(0, "a")],))
    thread.start()
    for _ in range(200):
        if any(isinstance(e, BatchEvent) and e.status == "retrying" for e in events):
            break
        threading.Event().wait(0.02)
    runner.cancel()
    thread.join(5)
    assert not thread.is_alive() and runner.results[0][0] == "cancelled"
    assert any(isinstance(e, LogEvent) and "retrying in" in e.message for e in events)
