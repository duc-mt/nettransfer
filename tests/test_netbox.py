"""NetBox / Nautobot inventory provider: response parsing, query building, safety properties, HTTP layer, CLI."""

import json
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

import pytest

from app import cli
from app.core import inventory
from app.core.batch import BatchFileError
from app.core.http_service import _Server, make_self_signed
from app.core.inventory import (
    NetBoxQuery,
    fetch_netbox,
    filter_targets,
    make_http_fetcher,
    parse_devices,
    query_params,
    split_values,
)

BASE = "https://nb.example.com"
TOKEN = "s3cr3t-token-value"


def nb_device(
    name="sw1", ip="10.1.0.1/24", platform="cisco-ios", site="dctt", role="core", tenant="cmc", tags=("edge",)
):
    """A NetBox 4.x style device."""
    return {
        "id": 1,
        "name": name,
        "primary_ip": {"id": 7, "address": ip, "family": {"value": 4}} if ip else None,
        "platform": {"name": platform.title(), "slug": platform} if platform else None,
        "site": {"name": site.upper(), "slug": site},
        "role": {"name": role, "slug": role},
        "tenant": {"name": tenant, "slug": tenant} if tenant else None,
        "tags": [{"name": t, "slug": t} for t in tags],
        "status": {"value": "active", "label": "Active"},
    }


def page(*devices, nxt=None):
    return {"count": len(devices), "next": nxt, "previous": None, "results": list(devices)}


class FakeApi:
    """Callable standing in for the HTTP layer: records every (url, headers) and answers from a script."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# ------------------------------------------------------------------ parsing
def test_parse_netbox_devices_into_entries_tags_and_vendors():
    entries, skipped = parse_devices(
        page(
            nb_device("sw-dctt-1", "10.1.0.1/24", "cisco-ios", "dctt", "core", "cmc", ("edge", "lab")),
            nb_device("fw-shtp-1", "10.2.0.9/32", "fortinet-fortios", "shtp", "firewall", "cmc", ()),
            nb_device("jr1", "10.3.0.1/24", "juniper-junos", "vptt", "core", None),
            nb_device("xr1", "10.4.0.1/24", "cisco-ios-xr", "hni", "core"),
            nb_device("odd", "10.5.0.1/24", "mikrotik-routeros", "cmt8", "edge"),
        )
    )
    by_name = {t.split(":", 1)[1]: e for e in entries for t in e["tags"] if t.startswith("name:")}
    sw = by_name["sw-dctt-1"]
    assert sw["host"] == "10.1.0.1" and sw["vendor"] == "cisco-ios"  # the /24 is not part of the address
    assert {
        "site:dctt",
        "role:core",
        "platform:cisco-ios",
        "tenant:cmc",
        "tag:edge",
        "tag:lab",
        "status:active",
    } <= set(sw["tags"])
    assert by_name["fw-shtp-1"]["vendor"] == "fortinet" and by_name["fw-shtp-1"]["host"] == "10.2.0.9"
    assert by_name["jr1"]["vendor"] == "junos" and not any(t.startswith("tenant:") for t in by_name["jr1"]["tags"])
    assert by_name["xr1"]["vendor"] == "" and by_name["odd"]["vendor"] == ""  # no profile: never guessed
    assert skipped == []


def test_devices_without_a_usable_primary_ip_are_skipped_with_a_reason():
    ipv6_only = nb_device("v6", None)
    ipv6_only["primary_ip6"] = {"address": "2001:db8::10/64"}
    broken = nb_device("bad", "not-an-ip")
    entries, skipped = parse_devices(page(nb_device("noip", None), ipv6_only, broken, nb_device("ok")))
    assert [e["host"] for e in entries] == ["2001:db8::10", "10.1.0.1"]
    assert skipped == [("noip", "no primary IP address"), ("bad", "invalid primary IP 'not-an-ip'")]


def test_older_netbox_uses_device_role_and_nautobot_uses_location_and_plain_tag_names():
    old = nb_device("sw")
    old["device_role"] = old.pop("role")
    old["role"] = None
    nautobot = nb_device("nb2")
    nautobot.pop("site")
    nautobot["location"] = {"name": "DCTT-Floor2"}
    nautobot["tags"] = [{"name": "Edge Router"}]
    entries, _ = parse_devices(page(old, nautobot))
    assert "role:core" in entries[0]["tags"]
    assert "site:DCTT-Floor2" in entries[1]["tags"] and "tag:Edge Router" in entries[1]["tags"]


@pytest.mark.parametrize("payload", [{}, {"results": "nope"}, {"results": None}])
def test_unexpected_payloads_are_rejected_not_crashed_on(payload):
    with pytest.raises(BatchFileError, match="results"):
        parse_devices(payload)


def test_non_dict_items_are_ignored():
    entries, skipped = parse_devices({"results": ["junk", 5, None, nb_device("ok")]})
    assert len(entries) == 1 and skipped == []


# ------------------------------------------------------------------ query building
def test_query_params_for_netbox_and_nautobot():
    q = NetBoxQuery(
        BASE, TOKEN, sites=("dctt", "shtp"), tags=("edge",), roles=("core",), platforms=("cisco-ios",), tenants=("cmc",)
    )
    assert query_params(q) == [
        ("site", "dctt"), ("site", "shtp"), ("tag", "edge"), ("role", "core"), ("platform", "cisco-ios"),
        ("tenant", "cmc"), ("status", "active"), ("limit", "100"),
    ]  # fmt: skip
    n = NetBoxQuery(BASE, TOKEN, kind="nautobot", sites=("DCTT",), tags=("Edge",), status="")
    assert query_params(n) == [("location", "DCTT"), ("tags", "Edge"), ("limit", "100"), ("depth", "1")]
    with pytest.raises(BatchFileError, match="unknown inventory server type"):
        query_params(NetBoxQuery(BASE, TOKEN, kind="phpipam"))


def test_split_values():
    assert split_values("a, b  c,,d") == ("a", "b", "c", "d") and split_values("") == ()


def test_the_token_is_never_part_of_a_repr():
    assert TOKEN not in repr(NetBoxQuery(BASE, TOKEN)) and TOKEN not in str(NetBoxQuery(BASE, TOKEN))


# ------------------------------------------------------------------ fetch_netbox with mock responses
def test_fetch_builds_the_request_and_returns_targets():
    api = FakeApi(page(nb_device("sw1"), nb_device("sw2", "10.1.0.2/24", "arista-eos")))
    result = fetch_netbox(NetBoxQuery(BASE, TOKEN, sites=("dctt",), roles=("core",)), api)
    ((url, headers),) = api.calls
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BASE}/api/dcim/devices/"
    assert urllib.parse.parse_qs(parts.query) == {
        "site": ["dctt"],
        "role": ["core"],
        "status": ["active"],
        "limit": ["100"],
    }
    assert headers["Authorization"] == f"Token {TOKEN}" and headers["Accept"] == "application/json"
    assert [(t.host, t.vendor) for t in result.targets] == [("10.1.0.1", "cisco-ios"), ("10.1.0.2", "arista-eos")]
    assert [t.index for t in result.targets] == [0, 1] and result.total == 2 and result.warnings == []
    # the imported tags work with the same filter language as every other inventory source
    assert [t.host for t in filter_targets(result.targets, "vendor:arista-eos site:dctt")] == ["10.1.0.2"]


def test_a_path_prefix_in_the_url_is_kept():
    api = FakeApi(page(nb_device()))
    fetch_netbox(NetBoxQuery("https://corp.example.com/netbox/", TOKEN), api)
    assert api.calls[0][0].startswith("https://corp.example.com/netbox/api/dcim/devices/?")


def test_netbox_v4_bearer_tokens_use_the_bearer_scheme():
    api = FakeApi(page(nb_device()))
    fetch_netbox(NetBoxQuery(BASE, "nbt_abc123.secretpart"), api)
    assert api.calls[0][1]["Authorization"] == "Bearer nbt_abc123.secretpart"


def test_pagination_follows_next_but_never_leaves_the_origin_the_operator_typed():
    api = FakeApi(
        page(nb_device("a"), nxt="http://evil.example/api/dcim/devices/?limit=100&offset=100&site=dctt"),
        page(nb_device("b", "10.1.0.2/24"), nxt=None),
    )
    result = fetch_netbox(NetBoxQuery(BASE, TOKEN, sites=("dctt",)), api)
    assert [t.host for t in result.targets] == ["10.1.0.1", "10.1.0.2"]
    second_url, second_headers = api.calls[1]
    assert second_url == f"{BASE}/api/dcim/devices/?limit=100&offset=100&site=dctt"  # re-based: token goes nowhere else
    assert second_headers["Authorization"] == f"Token {TOKEN}"


def test_nautobot_style_ip_links_are_resolved_on_the_same_origin_and_cached():
    linked = nb_device("n1", None)
    linked["primary_ip4"] = {"id": "u1", "url": "http://internal.nautobot.local/api/ipam/ip-addresses/u1/"}
    twin = nb_device("n2", None)
    twin["primary_ip4"] = {"id": "u1", "url": "http://internal.nautobot.local/api/ipam/ip-addresses/u1/"}
    api = FakeApi(page(linked, twin), {"address": "10.9.0.1/24"})
    result = fetch_netbox(NetBoxQuery(BASE, TOKEN, kind="nautobot"), api)
    assert [t.host for t in result.targets] == ["10.9.0.1", "10.9.0.1"]
    assert len(api.calls) == 2 and api.calls[1][0] == f"{BASE}/api/ipam/ip-addresses/u1/"  # one lookup, same origin


def test_an_unresolvable_ip_link_means_skipped_not_a_crash():
    linked = nb_device("n1", None)
    linked["primary_ip4"] = {"id": "u1", "url": f"{BASE}/api/ipam/ip-addresses/u1/"}
    api = FakeApi(page(linked, nb_device("ok")), BatchFileError(["HTTP 404"]))
    result = fetch_netbox(NetBoxQuery(BASE, TOKEN), api)
    assert [t.host for t in result.targets] == ["10.1.0.1"] and result.skipped == [("n1", "no primary IP address")]


def test_skipped_devices_are_reported_alongside_the_targets():
    result = fetch_netbox(NetBoxQuery(BASE, TOKEN), FakeApi(page(nb_device("noip", None), nb_device("ok"))))
    assert result.skipped == [("noip", "no primary IP address")] and result.total == 2 and len(result.targets) == 1


@pytest.mark.parametrize(
    ("query", "answers", "message"),
    [
        (NetBoxQuery(BASE, ""), [], "token is required"),
        (NetBoxQuery("netbox.example.com", TOKEN), [], "must look like"),
        (NetBoxQuery("ftp://nb.example.com", TOKEN), [], "must look like"),
        (NetBoxQuery(BASE, TOKEN), [page()], "no devices matched"),
        (
            NetBoxQuery(BASE, TOKEN),
            [page(nb_device("a", None), nb_device("b", None))],
            "2 matched but have no usable primary IP",
        ),
        (NetBoxQuery(BASE, TOKEN), [{"detail": "oops"}], "results"),
        (NetBoxQuery(BASE, TOKEN), [BatchFileError(["the API rejected the token (HTTP 403)"])], "rejected the token"),
    ],
)
def test_error_cases_are_clear_and_never_mention_the_token(query, answers, message):
    with pytest.raises(BatchFileError, match=message) as exc:
        fetch_netbox(query, FakeApi(*answers))
    assert TOKEN not in str(exc.value)


def test_plain_http_and_disabled_tls_verification_produce_warnings():
    http = fetch_netbox(NetBoxQuery("http://nb.local", TOKEN), FakeApi(page(nb_device())))
    assert any("unencrypted" in w for w in http.warnings)
    insecure = fetch_netbox(NetBoxQuery(BASE, TOKEN, verify_tls=False), FakeApi(page(nb_device())))
    assert any("verification is OFF" in w for w in insecure.warnings)
    assert fetch_netbox(NetBoxQuery(BASE, TOKEN), FakeApi(page(nb_device()))).warnings == []


def test_the_import_is_capped_with_a_warning(monkeypatch):
    monkeypatch.setattr(inventory, "MAX_DEVICES", 2)
    pages = [
        page(
            nb_device(f"d{i}", f"10.0.0.{i}/24"),
            nb_device(f"e{i}", f"10.0.1.{i}/24"),
            nxt=f"{BASE}/api/dcim/devices/?offset={i}",
        )
        for i in range(1, 6)
    ]
    result = fetch_netbox(NetBoxQuery(BASE, TOKEN), FakeApi(*pages))
    assert len(result.targets) == 2 and any("stopped after 2 devices" in w for w in result.warnings)


# ------------------------------------------------------------------ the real HTTP layer, against a local server
class Api:
    """Tiny NetBox look-alike. ``mode``: ok | redirect | html | slow."""

    def __init__(self, token=TOKEN, pages=None, mode="ok", redirect_to=""):
        self.token, self.mode, self.redirect_to = token, mode, redirect_to
        self.pages = pages if pages is not None else [page(nb_device("sw1"))]
        self.requests = []  # (path+query, authorization header)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization", "")))
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                    return
                if outer.mode == "slow":
                    time.sleep(2)
                if outer.mode == "html":
                    body, status = b"<html>login</html>", 200
                elif self.headers.get("Authorization") != f"Token {outer.token}":
                    body, status = b'{"detail": "Invalid token"}', 401
                else:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    index = int(query.get("offset", ["0"])[0])
                    data = outer.pages[index]
                    if data.get("next") == "AUTO":  # next link pointing at a made-up public host
                        data = {**data, "next": f"http://netbox.invalid/api/dcim/devices/?offset={index + 1}&limit=100"}
                    body, status = json.dumps(data).encode(), 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if outer.mode != "html" else "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = _Server(("127.0.0.1", 0), Handler)  # the shared no-DNS-lookup server class
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    servers = []

    def make(**kw):
        servers.append(Api(**kw))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_real_http_import_sends_the_token_only_in_the_header(api):
    server = api(pages=[page(nb_device("a"), nxt="AUTO"), page(nb_device("b", "10.1.0.2/24"))])
    result = fetch_netbox(NetBoxQuery(server.url, TOKEN, sites=("dctt",)))
    assert [t.host for t in result.targets] == ["10.1.0.1", "10.1.0.2"]
    assert all(auth == f"Token {TOKEN}" for _path, auth in server.requests) and len(server.requests) == 2
    assert all(TOKEN not in path for path, _auth in server.requests)  # never in a URL
    assert "offset=1" in server.requests[1][0]  # followed the page link, re-based onto the real server


def test_real_http_wrong_token_gives_a_clear_message_without_the_token(api):
    server = api(token="a-different-token")
    with pytest.raises(BatchFileError, match="rejected the token") as exc:
        fetch_netbox(NetBoxQuery(server.url, TOKEN))
    assert TOKEN not in str(exc.value)


def test_real_http_redirects_are_refused_and_the_token_is_not_forwarded(api):
    elsewhere = api()  # a second server the redirect points at
    redirecting = api(mode="redirect", redirect_to=f"{elsewhere.url}/api/dcim/devices/")
    with pytest.raises(BatchFileError, match="redirected"):
        fetch_netbox(NetBoxQuery(redirecting.url, TOKEN))
    assert elsewhere.requests == []  # the token never reached the other host


def test_real_http_non_json_and_unreachable_and_timeout(api):
    with pytest.raises(BatchFileError, match="did not return JSON"):
        fetch_netbox(NetBoxQuery(api(mode="html").url, TOKEN))
    closed = api()
    url = closed.url
    closed.close()
    with pytest.raises(BatchFileError, match="cannot reach"):
        fetch_netbox(NetBoxQuery(url, TOKEN, timeout=3))
    with pytest.raises(BatchFileError, match="no answer"):
        fetch_netbox(NetBoxQuery(api(mode="slow").url, TOKEN, timeout=0.5))


@pytest.fixture
def https_api(tmp_path):
    cert, key = make_self_signed("127.0.0.1")
    (tmp_path / "ca.pem").write_bytes(cert)
    (tmp_path / "srv.pem").write_bytes(cert + key)
    server = Api()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "srv.pem"))
    server.server.socket = context.wrap_socket(server.server.socket, server_side=True)
    server.url = server.url.replace("http://", "https://")
    yield server, str(tmp_path / "ca.pem")
    server.close()


def test_tls_certificates_are_verified_by_default_and_a_ca_bundle_fixes_it(https_api):
    server, ca = https_api
    with pytest.raises(BatchFileError, match="certificate verification failed"):
        fetch_netbox(NetBoxQuery(server.url, TOKEN))
    assert server.requests == []  # nothing (and so no token) was sent to an unverified server
    result = fetch_netbox(NetBoxQuery(server.url, TOKEN, ca_bundle=ca))
    assert len(result.targets) == 1 and result.warnings == []
    insecure = fetch_netbox(NetBoxQuery(server.url, TOKEN, verify_tls=False))
    assert len(insecure.targets) == 1 and any("verification is OFF" in w for w in insecure.warnings)
    with pytest.raises(BatchFileError, match="CA bundle"):
        make_http_fetcher(NetBoxQuery(server.url, TOKEN, ca_bundle="/no/such/file.pem"))


# ------------------------------------------------------------------ CLI
@pytest.fixture
def cli_netbox(monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)

    def install(*answers):
        fake = FakeApi(*answers)
        monkeypatch.setattr(inventory, "make_http_fetcher", lambda _q: fake)
        return fake

    return install


def test_cli_lists_netbox_devices_with_filters_and_never_prints_the_token(cli_netbox, capsys):
    fake = cli_netbox(
        page(
            nb_device("a", "10.1.0.1/24", "cisco-ios", "dctt"),
            nb_device("b", "10.2.0.1/24", "arista-eos", "shtp"),
            nb_device("c", None),
        )
    )
    args = [
        "batch",
        "scp",
        "fw.bin",
        "--netbox-url",
        BASE,
        "--nb-site",
        "dctt,shtp",
        "--nb-role",
        "core",
        "--nb-tag",
        "edge",
        "--list-targets",
    ]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert "10.1.0.1" in captured.out and "10.2.0.1" in captured.out and "2 device(s)" in captured.out
    assert "cisco-ios" in captured.out and "arista-eos" in captured.out
    assert "skipped 1 device(s): c (no primary IP address)" in captured.err
    assert TOKEN not in captured.out + captured.err
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][0]).query)
    assert query["site"] == ["dctt", "shtp"] and query["role"] == ["core"] and query["tag"] == ["edge"]


def test_cli_filter_applies_to_netbox_tags_and_status_any_drops_the_status_filter(cli_netbox, capsys):
    fake = cli_netbox(
        page(nb_device("a", "10.1.0.1/24", "cisco-ios", "dctt"), nb_device("b", "10.2.0.1/24", "arista-eos", "shtp"))
    )
    args = [
        "batch",
        "scp",
        "fw.bin",
        "--netbox-url",
        BASE,
        "--nb-status",
        "any",
        "--filter",
        "vendor:arista-eos",
        "--list-targets",
    ]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "10.2.0.1" in out and "10.1.0.1" not in out
    assert "status" not in urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][0]).query)


def test_cli_nautobot_kind_uses_nautobot_filters(cli_netbox):
    fake = cli_netbox(page(nb_device("a")))
    assert (
        cli.main(
            [
                "batch",
                "scp",
                "f",
                "--netbox-url",
                BASE,
                "--netbox-kind",
                "nautobot",
                "--nb-site",
                "DCTT",
                "--list-targets",
            ]
        )
        == 0
    )
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][0]).query)
    assert query["location"] == ["DCTT"] and query["depth"] == ["1"] and "site" not in query


def test_cli_errors_and_warnings(cli_netbox, monkeypatch, capsys):
    cli_netbox(page())
    assert cli.main(["batch", "scp", "f", "--netbox-url", BASE, "--list-targets"]) == 2
    assert "no devices matched" in capsys.readouterr().err
    monkeypatch.delenv("NETBOX_TOKEN")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="never a command-line option"):
        cli.main(["batch", "scp", "f", "--netbox-url", BASE, "--list-targets"])
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    cli_netbox(page(nb_device()))
    assert (
        cli.main(["batch", "scp", "f", "--netbox-url", "http://nb.local", "--netbox-insecure", "--list-targets"]) == 0
    )
    assert "unencrypted" in capsys.readouterr().err


def test_cli_sources_are_mutually_exclusive(tmp_path):
    csv = tmp_path / "t.csv"
    csv.write_text("host\n10.0.0.1\n")
    with pytest.raises(SystemExit):
        cli.main(["batch", "scp", "f", "--targets", str(csv), "--netbox-url", BASE])
