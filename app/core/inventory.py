"""Device inventories -> batch targets, with tags and filters.

Supported sources (detected from the file, or forced with ``fmt``):

* **csv** - the batch CSV (``host`` plus optional username/password/remote_path/port/vendor/tags)
* **ansible** - an Ansible YAML inventory (groups, ``hosts``, ``children``, ``ansible_host`` / ``ansible_user`` /
  ``ansible_port``; groups become ``group:<name>`` tags). Needs PyYAML (``pip install pyyaml``).
* **netmiko** - a YAML/JSON list of device dicts (``host`` or ``ip``, ``username``, ``port``, ``device_type``,
  optional ``tags``/``site``/``group``)
* **zabbix** - a Zabbix *Configuration -> Hosts -> Export* JSON file (host groups become ``group:`` tags, host tags
  become ``tag:value`` tags)

Tags are plain strings, conventionally ``key:value`` (``site:DCTT``, ``vendor:fortinet``).

Filter syntax (``--filter``): whitespace-separated terms, **all** must match; inside a term ``,`` means *or*; ``!``
negates; ``*``/``?`` are wildcards; case-insensitive. A term matches a tag or the host name:
``site:DCTT vendor:fortinet,juniper !group:lab host:10.1.*``.
"""

from __future__ import annotations

import fnmatch
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .batch import BatchFileError, BatchTarget, load_targets_csv, split_tags

# Netmiko ``device_type`` -> vendor profile id (only the ones we have a profile for)
NETMIKO_VENDORS = {
    "cisco_ios": "cisco-ios",
    "cisco_xe": "cisco-ios",
    "cisco_nxos": "cisco-nxos",
    "juniper": "junos",
    "juniper_junos": "junos",
    "huawei": "huawei",
    "fortinet": "fortinet",
    "linux": "linux",
}


# Ansible ``ansible_network_os`` (last dotted part, e.g. cisco.ios.ios -> ios) -> vendor profile id
ANSIBLE_VENDORS = {
    "ios": "cisco-ios",
    "iosxe": "cisco-ios",
    "nxos": "cisco-nxos",
    "junos": "junos",
    "vrp": "huawei",
    "ce": "huawei",
    "fortios": "fortinet",
}


def load_inventory(path: str, fmt: str = "auto") -> List[BatchTarget]:
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = _detect(path)
    if fmt == "csv":
        return load_targets_csv(path)
    data = _read_structured(path)
    if fmt == "zabbix":
        entries = _from_zabbix(data)
    elif fmt == "ansible":
        entries = _from_ansible(data)
    elif fmt == "netmiko":
        entries = _from_netmiko(data)
    else:
        raise BatchFileError([f"unknown inventory format '{fmt}' (csv, ansible, netmiko, zabbix, auto)"])
    return _to_targets(entries)


def _detect(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ""):
        return "csv"
    data = _read_structured(path)
    if isinstance(data, dict) and "zabbix_export" in data:
        return "zabbix"
    if isinstance(data, list):
        return "netmiko"
    return "ansible"


def _read_structured(path: str) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise BatchFileError([str(exc)]) from None
    if os.path.splitext(path)[1].lower() == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise BatchFileError([f"invalid JSON: {exc}"]) from None
    try:
        import yaml
    except ImportError:
        raise BatchFileError(["reading YAML inventories needs PyYAML: pip install pyyaml"]) from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BatchFileError([f"invalid YAML: {exc}"]) from None


Entry = Dict[str, Any]


def _from_ansible(data: Any) -> List[Entry]:
    if not isinstance(data, dict):
        raise BatchFileError(["an Ansible inventory must be a mapping of groups"])
    hosts: Dict[str, Entry] = {}

    def walk(group: str, body: Any, inherited: Dict[str, Any], groups: Tuple[str, ...]) -> None:
        body = body or {}
        merged = {**inherited, **(body.get("vars") or {})}
        here = groups + ((group,) if group not in ("all",) else ())
        for name, hostvars in (body.get("hosts") or {}).items():
            entry = hosts.setdefault(name, {"host": name, "tags": [], "vars": {}})
            entry["vars"].update({**merged, **(hostvars or {})})
            entry["tags"] += [f"group:{g}" for g in here if f"group:{g}" not in entry["tags"]]
        for child, child_body in (body.get("children") or {}).items():
            walk(child, child_body, merged, here)

    walk("all", data.get("all", {"children": data}), {}, ())
    out = []
    for name, entry in hosts.items():
        v = entry["vars"]
        out.append(
            {
                "host": str(v.get("ansible_host") or name),
                "username": v.get("ansible_user") or "",
                "port": v.get("ansible_port"),
                "vendor": ANSIBLE_VENDORS.get(str(v.get("ansible_network_os", "")).split(".")[-1].lower(), ""),
                "tags": entry["tags"] + ([f"name:{name}"] if v.get("ansible_host") else []),
            }
        )
    return out


def _from_netmiko(data: Any) -> List[Entry]:
    items = data.get("devices", data) if isinstance(data, dict) else data
    if isinstance(items, dict):  # {name: {device dict}}
        items = [{"name": k, **(v or {})} for k, v in items.items()]
    if not isinstance(items, list):
        raise BatchFileError(["a Netmiko inventory must be a list of device dicts"])
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise BatchFileError(["every device must be a mapping"])
        host = item.get("host") or item.get("ip") or item.get("hostname")
        tags = list(item.get("tags") or []) if not isinstance(item.get("tags"), str) else list(split_tags(item["tags"]))
        for key in ("site", "group", "role"):
            if item.get(key):
                tags.append(f"{key}:{item[key]}")
        if item.get("name"):
            tags.append(f"name:{item['name']}")
        out.append(
            {
                "host": host,
                "username": item.get("username") or item.get("user") or "",
                "port": item.get("port"),
                "vendor": NETMIKO_VENDORS.get(str(item.get("device_type", "")).lower(), item.get("vendor", "")),
                "tags": tags,
            }
        )
    return out


def _from_zabbix(data: Any) -> List[Entry]:
    try:
        hosts = data["zabbix_export"]["hosts"]
    except (KeyError, TypeError):
        raise BatchFileError(["not a Zabbix host export (no zabbix_export.hosts)"]) from None
    out = []
    for h in hosts:
        interfaces = h.get("interfaces") or []
        iface = interfaces[0] if interfaces else {}
        use_ip = str(iface.get("useip", "YES")).upper() in ("YES", "1")
        address = iface.get("ip") if use_ip else iface.get("dns")
        tags = [f"group:{g.get('name')}" for g in h.get("groups") or [] if g.get("name")]
        tags += [f"{t.get('tag')}:{t.get('value', '')}".rstrip(":") for t in h.get("tags") or [] if t.get("tag")]
        tags.append(f"name:{h.get('name') or h.get('host')}")
        out.append({"host": address or h.get("host"), "username": "", "port": None, "vendor": "", "tags": tags})
    return out


def _to_targets(entries: Iterable[Entry]) -> List[BatchTarget]:
    targets: List[BatchTarget] = []
    errors: List[str] = []
    for n, e in enumerate(entries, 1):
        if not e.get("host"):
            errors.append(f"entry {n}: no host/ip")
            continue
        port: Optional[int] = None
        if e.get("port") not in (None, ""):
            try:
                port = int(e["port"])
                if not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"entry {n} ({e['host']}): invalid port '{e['port']}'")
                continue
        tags = list(dict.fromkeys(str(t) for t in e.get("tags") or []))
        if e.get("vendor"):
            tags.append(f"vendor:{e['vendor']}")
        targets.append(
            BatchTarget(
                len(targets),
                str(e["host"]),
                str(e.get("username") or ""),
                "",
                "",
                port,
                e.get("vendor") or "",
                tuple(tags),
            )
        )
    if errors:
        raise BatchFileError(errors)
    if not targets:
        raise BatchFileError(["no devices found in the inventory"])
    return targets


def _term_matches(term: str, target: BatchTarget) -> bool:
    negate = term.startswith("!")
    term = term[1:] if negate else term
    candidates = [t.lower() for t in target.tags] + [f"host:{target.host}".lower(), target.host.lower()]
    if ":" in term:  # "vendor:a,b" means vendor:a OR vendor:b
        key, _, values = term.partition(":")
        alternatives = [f"{key}:{v}" for v in values.split(",") if v]
    else:
        alternatives = [a for a in term.split(",") if a]
    hit = any(any(fnmatch.fnmatchcase(c, alt.lower()) for c in candidates) for alt in alternatives)
    return not hit if negate else hit


def filter_targets(targets: Sequence[BatchTarget], expression: str) -> List[BatchTarget]:
    """Apply a ``--filter`` expression; the result is re-indexed from 0 (the batch UI/CLI index by position)."""
    terms = (expression or "").split()
    kept = [t for t in targets if all(_term_matches(term, t) for term in terms)]
    return [
        BatchTarget(i, t.host, t.username, t.password, t.remote_path, t.port, t.vendor, t.tags)
        for i, t in enumerate(kept)
    ]
