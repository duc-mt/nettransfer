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
import ipaddress
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .batch import BatchFileError, BatchTarget, load_targets_csv, split_tags
from .vendors import vendor_from_platform

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


# ------------------------------------------------------------------------------------------------------------------
# Dynamic inventory: NetBox / Nautobot REST API
#
# ``fetch_netbox`` pages through ``/api/dcim/devices/`` with an API token and turns each device into a BatchTarget
# (primary IP -> host, platform -> vendor profile, site/role/platform/tenant/tags -> filterable tags). Security notes:
# the token is only ever sent to the origin the operator typed (redirects are refused and "next"/nested links are
# re-based onto that origin, never followed to another host), it is never logged or stored by this module, and TLS
# certificates are verified unless the caller explicitly turns that off.
# ------------------------------------------------------------------------------------------------------------------
NETBOX_KINDS = ("netbox", "nautobot")
PAGE_SIZE = 100
MAX_DEVICES = 5000
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
Fetcher = Callable[[str, Mapping[str, str]], Mapping[str, Any]]  # (url, headers) -> parsed JSON object


def split_values(text: str) -> Tuple[str, ...]:
    """``"a, b  c"`` -> ("a", "b", "c"): filter fields accept commas and/or spaces between values."""
    return tuple(v for v in re.split(r"[,\s]+", text or "") if v)


@dataclass(frozen=True)
class NetBoxQuery:
    url: str  # the server root, e.g. https://netbox.example.com
    token: str = field(default="", repr=False)
    kind: str = "netbox"  # "netbox" (also Nautobot 1.x) or "nautobot" (2.x: location/tags/depth)
    sites: Tuple[str, ...] = ()  # NetBox: slugs. Nautobot 2.x: location names
    tags: Tuple[str, ...] = ()
    roles: Tuple[str, ...] = ()
    platforms: Tuple[str, ...] = ()
    tenants: Tuple[str, ...] = ()
    status: str = "active"  # "" = any status
    verify_tls: bool = True
    ca_bundle: str = ""
    timeout: float = 20.0


@dataclass
class NetBoxResult:
    targets: List[BatchTarget]
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (device name, why it cannot be a target)
    warnings: List[str] = field(default_factory=list)
    total: int = 0  # devices the API returned


def _base_url(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise BatchFileError(["the NetBox URL must look like https://netbox.example.com"])
    return parts


def query_params(q: NetBoxQuery) -> List[Tuple[str, str]]:
    if q.kind not in NETBOX_KINDS:
        raise BatchFileError([f"unknown inventory server type '{q.kind}' (netbox, nautobot)"])
    names = {"site": "site", "tag": "tag", "role": "role", "platform": "platform", "tenant": "tenant"}
    if q.kind == "nautobot":
        names.update({"site": "location", "tag": "tags"})
    params: List[Tuple[str, str]] = []
    for key, values in (
        ("site", q.sites),
        ("tag", q.tags),
        ("role", q.roles),
        ("platform", q.platforms),
        ("tenant", q.tenants),
    ):
        params += [(names[key], v) for v in values]
    if q.status:
        params.append(("status", q.status))
    params.append(("limit", str(PAGE_SIZE)))
    if q.kind == "nautobot":
        params.append(("depth", "1"))  # expand primary_ip4 etc. so the address is in the reply
    return params


def _auth_headers(token: str) -> Dict[str, str]:
    scheme = "Bearer" if token.startswith("nbt_") else "Token"  # NetBox 4 v2 tokens are bearer tokens
    return {"Authorization": f"{scheme} {token}", "Accept": "application/json", "User-Agent": "NetTransfer"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:  # never forward the token to another URL
        return None


def make_http_fetcher(q: NetBoxQuery) -> Fetcher:
    base = _base_url(q.url)
    context: Optional[ssl.SSLContext] = None
    if base.scheme == "https":
        try:
            context = ssl.create_default_context(cafile=q.ca_bundle or None)
        except (OSError, ssl.SSLError) as exc:
            raise BatchFileError([f"cannot load the CA bundle '{q.ca_bundle}': {exc}"]) from None
        if not q.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))

    def fetch(url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with opener.open(request, timeout=q.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise BatchFileError([_http_error_message(exc.code)]) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise BatchFileError(
                    [f"TLS certificate verification failed ({reason.verify_message}): provide the CA bundle, or "
                     "turn verification off only on a network you trust"]
                ) from None  # fmt: skip
            raise BatchFileError([f"cannot reach {base.netloc}: {reason}"]) from None
        except TimeoutError:
            raise BatchFileError([f"no answer from {base.netloc} within {q.timeout:g}s"]) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise BatchFileError(["the API reply is unreasonably large - narrow the filters"])
        try:
            data = json.loads(body)
        except ValueError:
            raise BatchFileError(["the server did not return JSON - is the URL the root of NetBox/Nautobot?"]) from None
        if not isinstance(data, dict):
            raise BatchFileError(["unexpected API reply (not a JSON object)"])
        return data

    return fetch


def _http_error_message(code: int) -> str:
    if code in (401, 403):
        return f"the API rejected the token (HTTP {code}): check the token and its permissions"
    if code == 404:
        return "HTTP 404: check the URL (it should be the server root, e.g. https://netbox.example.com)"
    if code in (301, 302, 303, 307, 308):
        return f"the server redirected the request (HTTP {code}): use its final URL (usually https://...)"
    return f"the API answered HTTP {code}"


def _label(obj: Any) -> str:
    """slug, else name, else value, of a nested NetBox/Nautobot object (or a plain string)."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("slug", "name", "value", "display"):
            if obj.get(key):
                return str(obj[key])
    return ""


def _rebase(url: str, base: urllib.parse.SplitResult) -> str:
    """Same path+query on the origin the operator typed: a 'next' or nested link can never redirect the token."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def _primary_ip(item: Mapping[str, Any], resolve: Callable[[str], str]) -> Tuple[str, str]:
    """(address, "") or ("", reason). NetBox embeds the address; Nautobot may only link to the IP object."""
    for key in ("primary_ip", "primary_ip4", "primary_ip6"):
        ip = item.get(key)
        if not ip:
            continue
        address = ""
        if isinstance(ip, dict):
            address = str(ip.get("address") or ip.get("host") or "")
            if not address and ip.get("url"):
                address = resolve(str(ip["url"]))
        elif isinstance(ip, str) and ip.startswith(("http://", "https://")):
            address = resolve(ip)
        elif isinstance(ip, str):
            address = ip
        if not address:
            continue
        try:
            return str(ipaddress.ip_interface(address.strip()).ip), ""
        except ValueError:
            return "", f"invalid primary IP '{address}'"
    return "", "no primary IP address"


def parse_devices(
    payload: Mapping[str, Any], resolve: Callable[[str], str] = lambda _url: ""
) -> Tuple[List[Entry], List[Tuple[str, str]]]:
    """One API page -> (target entries, skipped devices). Pure apart from ``resolve`` (the optional IP-link lookup)."""
    results = payload.get("results")
    if not isinstance(results, list):
        raise BatchFileError(["unexpected API reply: no 'results' list"])
    entries: List[Entry] = []
    skipped: List[Tuple[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("display") or f"device-{item.get('id', '?')}")
        host, reason = _primary_ip(item, resolve)
        if not host:
            skipped.append((name, reason))
            continue
        platform = _label(item.get("platform"))
        tags = [f"name:{name}"]
        for key, obj in (
            ("site", item.get("site") or item.get("location")),
            ("role", item.get("role") or item.get("device_role")),
            ("platform", item.get("platform")),
            ("tenant", item.get("tenant")),
            ("status", item.get("status")),
        ):
            if _label(obj):
                tags.append(f"{key}:{_label(obj)}")
        tags += [f"tag:{_label(t)}" for t in item.get("tags") or [] if _label(t)]
        entries.append(
            {"host": host, "username": "", "port": None, "vendor": vendor_from_platform(platform), "tags": tags}
        )
    return entries, skipped


def fetch_netbox(q: NetBoxQuery, fetch: Optional[Fetcher] = None) -> NetBoxResult:
    """Query the API and return importable targets. ``fetch`` is injectable so tests (and callers) need no network."""
    base = _base_url(q.url)
    params = query_params(q)
    if not q.token:
        raise BatchFileError(["an API token is required"])
    fetcher = fetch or make_http_fetcher(q)
    headers = _auth_headers(q.token)
    result = NetBoxResult([])
    if base.scheme == "http":
        result.warnings.append("the URL is plain http: the API token travels unencrypted")
    if not q.verify_tls and base.scheme == "https":
        result.warnings.append("TLS certificate verification is OFF")
    prefix = base.path.rstrip("/")
    url = urllib.parse.urlunsplit(
        (base.scheme, base.netloc, f"{prefix}/api/dcim/devices/", urllib.parse.urlencode(params), "")
    )
    ip_cache: Dict[str, str] = {}

    def resolve(link: str) -> str:
        if link not in ip_cache:
            try:
                ip_cache[link] = str(fetcher(_rebase(link, base), headers).get("address") or "")
            except BatchFileError:
                ip_cache[link] = ""
        return ip_cache[link]

    entries: List[Entry] = []
    seen_pages = 0
    while url:
        page = fetcher(url, headers)
        found, skipped = parse_devices(page, resolve)
        result.total += len(page.get("results") or [])
        entries += found
        result.skipped += skipped
        seen_pages += 1
        nxt = page.get("next")
        url = _rebase(str(nxt), base) if nxt else ""
        if url and (result.total >= MAX_DEVICES or seen_pages > MAX_DEVICES // PAGE_SIZE + 5):
            result.warnings.append(f"stopped after {result.total} devices: narrow the filters to import the rest")
            break
    if not entries:
        why = f" ({len(result.skipped)} matched but have no usable primary IP)" if result.skipped else ""
        raise BatchFileError([f"no devices matched the filters{why}"])
    result.targets = _to_targets(entries)
    return result
