"""Closed namespace fixtures for a synthetic retrieval boundary proof."""

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from primary_signal.retrieval import policy
from primary_signal.retrieval.fixture_rpc import FIXTURE_RPC_HOST, FIXTURE_RPC_ORIGIN
from scripts.ingestion_recovery_fixture import (
    ARTICLE_BODY,
    EXPECTED_TITLE,
)
from scripts.ingestion_recovery_fixture import (
    EXPECTED_NORMALIZED_HASH as EXPECTED_NORMALIZED_HASH,
)
from scripts.ingestion_recovery_fixture import (
    EXPECTED_TEXT as EXPECTED_TEXT,
)
from scripts.publication_smoke_fixture import INIT_FILES, local_docker_context, require

# Reuse the target policy snapshot instead of maintaining a second deny list.
V4_DENY = policy._V4_DENY  # pyright: ignore[reportPrivateUsage]

POLICY_TABLE = "synthetic_retrieval"
PUBLIC_IP = "8.8.8.8"
DNS_IP = "192.0.2.53"
DENY_IPS = ("127.0.0.2", "127.0.0.11", "10.23.45.67", "169.254.169.254", "198.51.100.10")
ALIASES = (PUBLIC_IP, DNS_IP, *DENY_IPS)
DENY_PORTS = (5432, 8001, 11434, 8080)
PUBLIC_HOST = "public.fixture.example.test"
PRIVATE_HOST = "private.fixture.example.test"
MIXED_HOST = "mixed.fixture.example.test"
FEED_URL = f"http://{PUBLIC_HOST}/feed.xml"
ARTICLE_URL = f"http://{PUBLIC_HOST}/article"
REDIRECT_URL = f"http://{PUBLIC_HOST}/redirect-private"
CONTROL_ORIGIN = "http://127.0.0.1:8766"
NETWORKS = ("retrieval_rpc", "retrieval_db")
EGRESS_NETWORK = "retrieval_egress"
TRANSIT_HOST = "example.com"
TRANSIT_URL = "https://example.com/"
UPSTREAM_DNS_IP = "1.1.1.1"
RPC_INTERFACE_PLACEHOLDER = "fixture_rpc"
EGRESS_INTERFACE_PLACEHOLDER = "fixture_egress"
VOLUME = "isolated-data"
SERVICES = {
    "database",
    "setup",
    "policy",
    "synthetic",
    "retriever",
    "feed_processor",
    "article_processor",
    "probe",
}
DATABASE = "primary_signal_isolated_retrieval_test"
BOOTSTRAP_ROLE = "isolated_bootstrap_test"
MIGRATION_ROLE = "isolated_migration_test"
FEED_ROLE = "isolated_feed_test"
ARTICLE_ROLE = "isolated_article_test"
BOOTSTRAP_PASSWORD = "public_isolated_bootstrap_fixture_only"  # noqa: S105  # pragma: allowlist secret
MIGRATION_PASSWORD = "public_isolated_migration_fixture_only"  # noqa: S105  # pragma: allowlist secret
FEED_PASSWORD = "public_isolated_feed_fixture_only"  # noqa: S105  # pragma: allowlist secret
ARTICLE_PASSWORD = "public_isolated_article_fixture_only"  # noqa: S105  # pragma: allowlist secret
RESOLVER = f"nameserver {DNS_IP}\noptions timeout:1 attempts:1 ndots:1\n"
POLICY_DOCKERFILE = (
    "FROM python@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83\n"
    "RUN apt-get update && apt-get install --yes --no-install-recommends nftables iproute2 "
    "&& rm -rf /var/lib/apt/lists/*\n"
)
FEED_BODY = (
    '<rss version="2.0"><channel><title>Synthetic boundary feed</title>'
    f"<link>http://{PUBLIC_HOST}/</link><description>Disposable fixture</description>"
    f"<item><title>{EXPECTED_TITLE}</title><link>{ARTICLE_URL}</link>"
    '<guid isPermaLink="false">synthetic-boundary-entry</guid></item></channel></rss>'
).encode()


def _match(left: object, right: object, op: str = "==") -> dict[str, object]:
    return {"match": {"op": op, "left": left, "right": right}}


def _meta(key: str) -> dict[str, object]:
    return {"meta": {"key": key}}


def _payload(protocol: str, key: str) -> dict[str, object]:
    return {"payload": {"protocol": protocol, "field": key}}


def _rules(
    *,
    transit_example: bool = False,
    rpc_interface: str | None = None,
    egress_interface: str | None = None,
) -> list[dict[str, Any]]:
    require(type(transit_example) is bool, "invalid transit fixture profile")
    if transit_example:
        require(
            all(
                isinstance(name, str)
                and re.fullmatch(r"[a-z][a-z0-9_.-]{0,14}", name)
                and name != "lo"
                for name in (rpc_interface, egress_interface)
            ),
            "invalid transit fixture interfaces",
        )
        require(rpc_interface != egress_interface, "fixture interfaces must differ")
    rules: list[dict[str, Any]] = []

    def add(chain: str, name: str, expressions: list[object], verdict: str) -> None:
        rules.append(
            {
                "family": "inet",
                "table": POLICY_TABLE,
                "chain": chain,
                "comment": name,
                "expr": [*expressions, {"counter": None}, {verdict: None}],
            }
        )

    # Exceptions are narrow fixture edges; no blanket loopback or established allow.
    add("output", "output_ipv6", [_match(_meta("nfproto"), "ipv6")], "drop")
    for protocol in ("udp", "tcp"):
        add(
            "output",
            f"output_dns_{protocol}",
            [_match(_payload("ip", "daddr"), DNS_IP), _match(_payload(protocol, "dport"), 53)],
            "accept",
        )
        add(
            "output",
            f"output_dns_reply_{protocol}",
            [
                _match(_payload("ip", "saddr"), DNS_IP),
                _match(_payload("ip", "daddr"), DNS_IP),
                _match(_payload(protocol, "sport"), 53),
                _match({"ct": {"key": "state"}}, "established"),
            ],
            "accept",
        )
    for port, label in ((8766, "control"),):
        add(
            "output",
            f"output_{label}",
            [
                _match(_meta("oifname"), "lo"),
                _match(_payload("ip", "daddr"), "127.0.0.1"),
                _match(_payload("tcp", "dport"), port),
            ],
            "accept",
        )
        add(
            "output",
            f"output_{label}_reply",
            [
                _match(_meta("oifname"), "lo"),
                _match(_payload("ip", "saddr"), "127.0.0.1"),
                _match(_payload("tcp", "sport"), port),
                _match(_payload("ip", "daddr"), "127.0.0.1"),
                _match({"ct": {"key": "state"}}, "established"),
            ],
            "accept",
        )
    add(
        "output",
        "output_rpc_reply",
        [
            _match(_payload("ip", "daddr"), {"set": ["192.0.2.11", "192.0.2.12"]}),
            _match(_payload("tcp", "sport"), 8765),
            _match({"ct": {"key": "state"}}, "established"),
        ],
        "accept",
    )
    add("output", "output_denied", [_match(_payload("ip", "daddr"), "@denied_v4")], "drop")
    add("output", "output_public", [_match(_payload("tcp", "dport"), {"set": [80, 443]})], "accept")
    add(
        "output",
        "output_source_reply",
        [
            _match(_payload("ip", "saddr"), PUBLIC_IP),
            _match(_payload("ip", "daddr"), PUBLIC_IP),
            _match(_payload("tcp", "sport"), {"set": [80, 443]}),
            _match({"ct": {"key": "state"}}, "established"),
        ],
        "accept",
    )
    add("output", "output_drop", [], "drop")
    add("input", "input_ipv6", [_match(_meta("nfproto"), "ipv6")], "drop")
    add(
        "input",
        "input_rpc",
        [
            _match(_payload("ip", "saddr"), {"set": ["192.0.2.11", "192.0.2.12"]}),
            _match(_payload("ip", "daddr"), FIXTURE_RPC_HOST),
            _match(_payload("tcp", "dport"), 8765),
        ],
        "accept",
    )
    for protocol in ("udp", "tcp"):
        add(
            "input",
            f"input_dns_{protocol}",
            [
                _match(_meta("iifname"), "lo"),
                _match(_payload("ip", "daddr"), DNS_IP),
                _match(_payload(protocol, "dport"), 53),
            ],
            "accept",
        )
        add(
            "input",
            f"input_dns_reply_{protocol}",
            [
                _match(_meta("iifname"), "lo"),
                _match(_payload("ip", "saddr"), DNS_IP),
                _match(_payload(protocol, "sport"), 53),
                _match({"ct": {"key": "state"}}, "established"),
            ],
            "accept",
        )
    add(
        "input",
        "input_control",
        [
            _match(_meta("iifname"), "lo"),
            _match(_payload("ip", "daddr"), "127.0.0.1"),
            _match(_payload("tcp", "dport"), 8766),
        ],
        "accept",
    )
    add(
        "input",
        "input_control_reply",
        [
            _match(_meta("iifname"), "lo"),
            _match(_payload("ip", "saddr"), "127.0.0.1"),
            _match(_payload("tcp", "sport"), 8766),
            _match(_payload("ip", "daddr"), "127.0.0.1"),
            _match({"ct": {"key": "state"}}, "established"),
        ],
        "accept",
    )
    add(
        "input",
        "input_source",
        [
            _match(_meta("iifname"), "lo"),
            _match(_payload("ip", "daddr"), PUBLIC_IP),
            _match(_payload("tcp", "dport"), {"set": [80, 443]}),
        ],
        "accept",
    )
    add(
        "input",
        "input_source_reply",
        [
            _match(_meta("iifname"), "lo"),
            _match(_payload("ip", "saddr"), PUBLIC_IP),
            _match(_payload("tcp", "sport"), {"set": [80, 443]}),
            _match({"ct": {"key": "state"}}, "established"),
        ],
        "accept",
    )
    add("input", "input_drop", [], "drop")
    add("forward", "forward_drop", [], "drop")
    if transit_example:
        for rule in rules:
            if rule["comment"] == "input_rpc":
                rule["expr"].insert(0, _match(_meta("iifname"), rpc_interface))
            elif rule["comment"] == "output_rpc_reply":
                rule["expr"].insert(0, _match(_meta("oifname"), rpc_interface))
            elif rule["comment"] == "output_public":
                rule["expr"].insert(0, _match(_meta("oifname"), {"set": ["lo", egress_interface]}))
        upstream: list[dict[str, Any]] = []
        for protocol in ("udp", "tcp"):
            add(
                "output",
                f"output_upstream_dns_{protocol}",
                [
                    _match(_meta("oifname"), egress_interface),
                    _match(_payload("ip", "daddr"), UPSTREAM_DNS_IP),
                    _match(_payload(protocol, "dport"), 53),
                ],
                "accept",
            )
            upstream.append(rules.pop())
            add(
                "input",
                f"input_upstream_dns_reply_{protocol}",
                [
                    _match(_meta("iifname"), egress_interface),
                    _match(_payload("ip", "saddr"), UPSTREAM_DNS_IP),
                    _match(_payload(protocol, "sport"), 53),
                    _match({"ct": {"key": "state"}}, "established"),
                ],
                "accept",
            )
            upstream.append(rules.pop())
        add(
            "input",
            "input_public_reply",
            [
                _match(_meta("iifname"), egress_interface),
                _match(_payload("ip", "saddr"), "@denied_v4", "!="),
                _match(_payload("tcp", "sport"), {"set": [80, 443]}),
                _match({"ct": {"key": "state"}}, "established"),
            ],
            "accept",
        )
        upstream.append(rules.pop())
        for rule in upstream:
            index = next(
                i
                for i, existing in enumerate(rules)
                if existing["comment"] == rule["chain"] + "_drop"
            )
            rules.insert(index, rule)
    return sorted(rules, key=lambda rule: ("input", "output", "forward").index(rule["chain"]))


def rules_source(
    *,
    transit_example: bool = False,
    rpc_interface: str | None = None,
    egress_interface: str | None = None,
) -> str:
    """An atomic transaction creates only a dedicated table in an owned namespace."""
    commands: list[dict[str, object]] = [
        {"add": {"table": {"family": "inet", "name": POLICY_TABLE}}}
    ]
    commands.append(
        {
            "add": {
                "set": {
                    "family": "inet",
                    "table": POLICY_TABLE,
                    "name": "denied_v4",
                    "type": "ipv4_addr",
                    "flags": ["interval"],
                    "elem": [
                        {"prefix": {"addr": str(network.network_address), "len": network.prefixlen}}
                        for network in V4_DENY
                    ],
                }
            }
        }
    )
    for name in ("input", "output", "forward"):
        commands.append(
            {
                "add": {
                    "chain": {
                        "family": "inet",
                        "table": POLICY_TABLE,
                        "name": name,
                        "type": "filter",
                        "hook": name,
                        "prio": 0,
                        "policy": "drop",
                    }
                }
            }
        )
    commands.extend(
        {"add": {"rule": rule}}
        for rule in _rules(
            transit_example=transit_example,
            rpc_interface=rpc_interface,
            egress_interface=egress_interface,
        )
    )
    return json.dumps({"nftables": commands})


def validate_policy_readback(
    value: object,
    *,
    transit_example: bool = False,
    rpc_interface: str | None = None,
    egress_interface: str | None = None,
) -> dict[str, int]:
    """Verify table semantics and return counters without leaking runtime addresses."""
    if isinstance(value, str):
        value = json.loads(value)
    document = cast(dict[str, Any], value)
    require(
        isinstance(value, dict) and set(document) == {"nftables"},
        "invalid namespace policy readback",
    )
    raw_entries = document["nftables"]
    require(isinstance(raw_entries, list), "invalid namespace policy entries")
    entries = cast(list[Any], raw_entries)
    observed_rules: list[dict[str, Any]] = []
    chains: dict[str, dict[str, Any]] = {}
    table_count = 0
    set_count = 0
    counters: dict[str, int] = {}
    for candidate in entries:
        entry = cast(dict[str, Any], candidate)
        require(isinstance(candidate, dict) and len(entry) == 1, "invalid namespace policy entry")
        kind, raw = next(iter(entry.items()))
        if kind == "metainfo":
            continue
        require(isinstance(raw, dict), "invalid namespace policy object")
        item = {key: val for key, val in cast(dict[str, Any], raw).items() if key != "handle"}
        if kind == "table":
            require(
                item == {"family": "inet", "name": POLICY_TABLE}, "namespace policy table changed"
            )
            table_count += 1
        elif kind == "chain":
            require(item["name"] not in chains, "duplicate namespace chain")
            chains[item["name"]] = item
        elif kind == "set":
            require(
                set(item) == {"family", "table", "name", "type", "flags", "elem"}
                and item.get("name") == "denied_v4"
                and item.get("family") == "inet"
                and item.get("table") == POLICY_TABLE
                and item.get("type") == "ipv4_addr"
                and item.get("flags") == ["interval"],
                "namespace deny set changed",
            )
            expected = {str(network) for network in V4_DENY}
            observed = {
                f"{v['prefix']['addr']}/{v['prefix']['len']}" if isinstance(v, dict) else v
                for v in item.get("elem", [])
            }
            require(
                tuple(ipaddress.collapse_addresses(ipaddress.IPv4Network(v) for v in observed))
                == tuple(ipaddress.collapse_addresses(ipaddress.IPv4Network(v) for v in expected)),
                "namespace deny ranges changed",
            )
            set_count += 1
        elif kind == "rule":
            expressions: list[dict[str, Any]] = []
            count = None
            for expression in item.get("expr", []):
                if "counter" in expression:
                    raw_counter = expression["counter"]
                    counter = cast(dict[str, Any], raw_counter)
                    require(
                        isinstance(raw_counter, dict)
                        and type(counter.get("packets")) is int
                        and type(counter.get("bytes")) is int
                        and counter["packets"] >= 0
                        and counter["bytes"] >= 0,
                        "invalid namespace counter",
                    )
                    count = cast(int, counter["packets"])
                    expressions.append({"counter": None})
                else:
                    expressions.append(expression)
            require(
                count is not None and item.get("comment") not in counters,
                "missing namespace counter",
            )
            counters[item["comment"]] = cast(int, count)
            item["expr"] = expressions
            observed_rules.append(item)
        else:
            require(False, "unknown namespace policy object")
    require(table_count == 1 and set_count == 1, "namespace policy object count changed")
    require(
        chains
        == {
            name: {
                "family": "inet",
                "table": POLICY_TABLE,
                "name": name,
                "type": "filter",
                "hook": name,
                "prio": 0,
                "policy": "drop",
            }
            for name in ("input", "output", "forward")
        },
        "namespace default policy changed",
    )
    require(
        observed_rules
        == _rules(
            transit_example=transit_example,
            rpc_interface=rpc_interface,
            egress_interface=egress_interface,
        ),
        "namespace policy rules changed",
    )
    return counters


def policy_source(*, transit_example: bool = False) -> str:
    import inspect

    require(type(transit_example) is bool, "invalid transit fixture profile")

    validator = inspect.getsource(validate_policy_readback)
    header = (
        "import ipaddress, json, pathlib, signal, subprocess\nfrom typing import Any, cast\n"
        f"POLICY_TABLE = {POLICY_TABLE!r}\nALIASES = {ALIASES!r}\n"
        f"V4_DENY = {tuple(str(network) for network in V4_DENY)!r}\n"
        f"TRANSIT_EXAMPLE = {transit_example!r}\n"
        f"UPSTREAM_DNS_IP = {UPSTREAM_DNS_IP!r}\n"
        f"EXPECTED_RULES = json.loads({json.dumps(_rules(transit_example=transit_example, rpc_interface=RPC_INTERFACE_PLACEHOLDER if transit_example else None, egress_interface=EGRESS_INTERFACE_PLACEHOLDER if transit_example else None))!r})\n"
        "def require(condition, message):\n    if not condition: raise RuntimeError(message)\n"
        "def _rules(**_kwargs):\n    return EXPECTED_RULES\n"
    )
    return (
        header
        + validator
        + r"""
ready = pathlib.Path("/tmp/policy-ready")
ready.unlink(missing_ok=True)
owned = json.loads(subprocess.check_output(["ip", "-j", "address", "show", "dev", "lo"], timeout=5))
assert len(owned) == 1
for address in ALIASES:
    matches = [entry for entry in owned[0]["addr_info"] if entry.get("local") == address]
    if matches:
        assert len(matches) == 1 and matches[0]["prefixlen"] == 32
    else:
        subprocess.run(["ip", "address", "add", address + "/32", "dev", "lo"], check=True, timeout=5)
for address in ALIASES:
    route = json.loads(subprocess.check_output(["ip", "-j", "route", "get", address], timeout=5))
    assert len(route) == 1 and route[0]["type"] == "local" and route[0]["dev"] == "lo"
interfaces = {"rpc": None, "egress": None}
if TRANSIT_EXAMPLE:
    links = json.loads(subprocess.check_output(["ip", "-j", "address", "show"], timeout=5))
    rpc = [link["ifname"] for link in links if any(entry.get("local") == "192.0.2.10" and entry.get("prefixlen") == 24 for entry in link.get("addr_info", []))]
    defaults = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "default"], timeout=5))
    assert len(rpc) == 1 and len(defaults) == 1
    egress = defaults[0]["dev"]
    owned_egress = [link for link in links if link["ifname"] == egress and any(entry.get("family") == "inet" and ipaddress.IPv4Address(entry["local"]) in ipaddress.IPv4Network("203.0.113.0/24") for entry in link.get("addr_info", []))]
    assert len(owned_egress) == 1 and egress != rpc[0] and egress != "lo"
    route = json.loads(subprocess.check_output(["ip", "-j", "route", "get", UPSTREAM_DNS_IP], timeout=5))
    assert len(route) == 1 and route[0]["dev"] == egress and route[0].get("type", "unicast") == "unicast"
    interfaces = {"rpc": rpc[0], "egress": egress}
    EXPECTED_RULES = json.loads(json.dumps(EXPECTED_RULES).replace("fixture_rpc", rpc[0]).replace("fixture_egress", egress))
transaction = json.loads(pathlib.Path("/fixture/rules.json").read_text())
if TRANSIT_EXAMPLE:
    transaction = json.loads(json.dumps(transaction).replace("fixture_rpc", interfaces["rpc"]).replace("fixture_egress", interfaces["egress"]))
prior = subprocess.run(["nft", "--json", "list", "table", "inet", POLICY_TABLE], capture_output=True, text=True, timeout=5, check=False)
if prior.returncode == 0:
    validate_policy_readback(prior.stdout)
    transaction["nftables"].insert(0, {"delete": {"table": {"family": "inet", "name": POLICY_TABLE}}})
body = json.dumps(transaction)
subprocess.run(["nft", "--json", "--check", "--file", "-"], input=body, text=True, check=True, timeout=5)
subprocess.run(["nft", "--json", "--file", "-"], input=body, text=True, check=True, timeout=5)
validate_policy_readback(subprocess.check_output(["nft", "--json", "list", "table", "inet", POLICY_TABLE], text=True, timeout=5))
pathlib.Path("/tmp/interfaces.json").write_text(json.dumps(interfaces))
ready.write_text("ready")
signal.pause()
"""
    )


def server_source(*, transit_example: bool = False) -> str:
    """Serve synthetic content and optional exact-host DNS forwarding."""
    require(type(transit_example) is bool, "invalid transit fixture profile")
    config = {
        "dns": DNS_IP,
        "transit": transit_example,
        "transit_host": TRANSIT_HOST,
        "upstream": UPSTREAM_DNS_IP,
        "public": PUBLIC_IP,
        "public_host": PUBLIC_HOST,
        "private_host": PRIVATE_HOST,
        "mixed_host": MIXED_HOST,
        "private": DENY_IPS[2],
        "deny_ips": DENY_IPS,
        "deny_ports": DENY_PORTS,
        "feed": FEED_BODY.decode(),
        "article": ARTICLE_BODY.decode(),
    }
    return r'''"""Credential-free bounded synthetic DNS and HTTP, with no forwarding."""
import json, socket, socketserver, struct, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
C = json.loads(__CONFIG__)
LOCK = threading.Lock()
STATE = {"dns_queries": 0, "source_requests": 0, "forbidden_requests": 0, "source_443_connections": 0}
if C["transit"]:
    STATE.update(upstream_udp_queries=0, upstream_tcp_queries=0, upstream_failures=0)
def increment(key):
    with LOCK:
        STATE[key] = min(STATE[key] + 1, 1000000)
def answer(data, *, tcp=False):
    if not 12 <= len(data) <= 512:
        return None
    ident, flags, questions, an, ns, ar = struct.unpack("!6H", data[:12])
    if flags & 0xF800 or questions != 1 or an or ns or ar:
        return None
    parts, pos = [], 12
    while pos < len(data):
        size = data[pos]; pos += 1
        if size == 0:
            break
        if size > 63 or pos + size > len(data):
            return None
        try:
            parts.append(data[pos:pos+size].decode("ascii").lower())
        except UnicodeError:
            return None
        pos += size
        if pos > 267:
            return None
    if pos + 4 != len(data):
        return None
    kind, cls = struct.unpack("!HH", data[pos:])
    if cls != 1:
        return None
    host = ".".join(parts)
    mapping = {C["public_host"]: [C["public"]], C["private_host"]: [C["private"]],
               C["mixed_host"]: [C["public"], C["private"]]}
    addresses = mapping.get(host)
    increment("dns_queries")
    if C["transit"] and host == C["transit_host"] and kind in (1, 28):
        return forward(data, tcp=tcp)
    records = addresses if addresses is not None and kind == 1 else []
    reply = struct.pack("!6H", ident, 0x8180 if addresses is not None else 0x8183, 1, len(records), 0, 0) + data[12:]
    for address in records:
        reply += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4) + socket.inet_aton(address)
    return reply
def forward(data, *, tcp):
    increment("upstream_tcp_queries" if tcp else "upstream_udp_queries")
    deadline = time.monotonic() + 2
    def receive(connection, length):
        value = b""
        while len(value) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection.settimeout(remaining)
            piece = connection.recv(length-len(value))
            if not piece:
                raise ValueError()
            value += piece
        return value
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as connection:
            connection.settimeout(2)
            connection.connect((C["upstream"], 53))
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection.settimeout(remaining)
            if tcp:
                connection.sendall(struct.pack("!H", len(data)) + data)
                length = struct.unpack("!H", receive(connection, 2))[0]
                if not 12 <= length <= 4096:
                    raise ValueError()
                response = receive(connection, length)
            else:
                connection.send(data)
                connection.settimeout(max(0.001, deadline-time.monotonic()))
                response = connection.recv(4097)
            if not 12 <= len(response) <= 4096:
                raise ValueError()
            header = struct.unpack("!6H", response[:12])
            if header[0] != struct.unpack("!H", data[:2])[0] or not header[1] & 0x8000 or header[1] & 0x7800 or header[2] != 1:
                raise ValueError()
            if response[12:len(data)] != data[12:]:
                raise ValueError()
            return response
    except (OSError, ValueError):
        increment("upstream_failures")
        return data[:2] + struct.pack("!5H", 0x8182, 1, 0, 0, 0) + data[12:]
class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        response = answer(data)
        if response is not None:
            connection.sendto(response, self.client_address)
class TCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        try:
            size = self.request.recv(2)
            if len(size) != 2:
                return
            length = struct.unpack("!H", size)[0]
            if not 12 <= length <= 512:
                return
            data = b""
            while len(data) < length:
                piece = self.request.recv(length-len(data))
                if not piece:
                    return
                data += piece
            response = answer(data, tcp=True)
            if response is not None:
                self.request.sendall(struct.pack("!H", len(response)) + response)
        except OSError:
            pass
class Source443(socketserver.BaseRequestHandler):
    def handle(self):
        increment("source_443_connections")
class Forbidden(socketserver.BaseRequestHandler):
    def handle(self):
        increment("forbidden_requests")
class FixtureTCPServer(socketserver.TCPServer):
    allow_reuse_address = True
class IPv6Server(FixtureTCPServer):
    address_family = socket.AF_INET6
class BoundedHTTP(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4
    def __init__(self, address, handler):
        self.capacity = threading.BoundedSemaphore(4)
        super().__init__(address, handler)
    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.capacity.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()
class HTTP(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)
    def log_message(self, *_args):
        pass
    def do_GET(self):
        if self.server.server_address[0] == "127.0.0.1":
            if self.path != "/state":
                self.send_error(404); return
            with LOCK:
                body = json.dumps(STATE).encode()
            content_type, status = "application/json", 200
        else:
            increment("source_requests")
            if self.headers.get("Host") not in (C["public_host"], C["public_host"] + ":80"):
                increment("forbidden_requests")
                self.send_error(400); return
            if self.path == "/redirect-private":
                self.send_response(302)
                self.send_header("Location", "http://" + C["private_host"] + "/article")
                self.send_header("Content-Length", "0")
                self.end_headers(); return
            if self.path not in ("/article", "/feed.xml"):
                self.send_error(404); return
            body = C["article" if self.path == "/article" else "feed"].encode()
            content_type, status = ("text/html" if self.path == "/article" else "application/rss+xml"), 200
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
if __name__ == "__main__":
    servers = [socketserver.UDPServer((C["dns"], 53), UDP),
               FixtureTCPServer((C["dns"], 53), TCP),
               FixtureTCPServer((C["public"], 443), Source443),
               IPv6Server(("::1", 80), Forbidden),
               BoundedHTTP((C["public"], 80), HTTP),
               BoundedHTTP(("127.0.0.1", 8766), HTTP)]
    servers += [FixtureTCPServer((address, port), Forbidden) for address in C["deny_ips"] for port in (80, 443)]
    servers += [FixtureTCPServer((C["public"], port), Forbidden) for port in C["deny_ports"]]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()
'''.replace("__CONFIG__", repr(json.dumps(config)))


@dataclass(frozen=True)
class IsolatedRetrievalFixture:
    root: Path
    directory: Path
    project: str
    image: str
    policy_image: str
    docker_args: tuple[str, ...]
    compose_args: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    expected_compose: dict[str, Any] = field(repr=False)
    expected_files: dict[str, str] = field(repr=False)
    transit_example: bool = False
    network_names: tuple[str, ...] = NETWORKS
    database_name: str = DATABASE
    bootstrap_role: str = BOOTSTRAP_ROLE
    migration_role: str = MIGRATION_ROLE
    feed_role: str = FEED_ROLE
    article_role: str = ARTICLE_ROLE


def _login_sql() -> str:
    rows = ["-- PUBLIC SYNTHETIC LOGINS; DISPOSABLE FIXTURE ONLY.\n"]
    for role, password in (
        (MIGRATION_ROLE, MIGRATION_PASSWORD),
        (FEED_ROLE, FEED_PASSWORD),
        (ARTICLE_ROLE, ARTICLE_PASSWORD),
    ):
        rows.append(
            f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;\n"
        )
    rows.extend(
        [
            f"GRANT CREATE ON DATABASE {DATABASE} TO {MIGRATION_ROLE},primary_signal_public_owner;\n",
            f"GRANT primary_signal_public_owner TO {MIGRATION_ROLE};\n",
            f"GRANT primary_signal_cap_queue_consume,primary_signal_cap_queue_submit,primary_signal_cap_feed_poll TO {FEED_ROLE};\n",
            f"GRANT primary_signal_cap_queue_consume,primary_signal_cap_article_persist TO {ARTICLE_ROLE};\n",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n",
        ]
    )
    return "".join(rows)


def _db_environment(role: str, password: str) -> dict[str, str]:
    return {
        "PRIMARY_SIGNAL_ENVIRONMENT": "test",
        "PRIMARY_SIGNAL_DATABASE_URL": f"postgresql+psycopg://{role}:{password}@198.51.100.10:5432/{DATABASE}",
        "PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE": role,
    }


def create_fixture(
    root: Path,
    directory: Path,
    project: str,
    *,
    image: str,
    policy_image: str,
    transit_example: bool = False,
) -> IsolatedRetrievalFixture:
    require(type(transit_example) is bool, "invalid transit fixture profile")
    root, directory = root.resolve(), directory.resolve()
    require(
        directory.is_relative_to(Path("/tmp")) and not directory.is_relative_to(root),  # noqa: S108 - caller creates private random directory.
        "isolated fixture must be private and outside checkout",
    )
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "invalid isolated fixture project")
    for candidate in (image, policy_image):
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9_.-]*:[a-z0-9][a-z0-9_.-]*", candidate),
            "isolated fixture requires tagged local images",
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not any(directory.iterdir()), "isolated fixture directory must be empty")
    directory.chmod(0o700)
    docker_args, env = local_docker_context(directory)
    files = {
        "policy.py": policy_source(transit_example=transit_example),
        "rules.json": rules_source(
            transit_example=transit_example,
            rpc_interface=RPC_INTERFACE_PLACEHOLDER if transit_example else None,
            egress_interface=EGRESS_INTERFACE_PLACEHOLDER if transit_example else None,
        ),
        "server.py": server_source(transit_example=transit_example),
        "resolv.conf": RESOLVER,
        "bootstrap-logins.sql": _login_sql(),
        "database_password": BOOTSTRAP_PASSWORD + "\n",
        "Dockerfile.policy": POLICY_DOCKERFILE,
    }
    for filename, body in files.items():
        path = directory / filename
        path.write_text(body)
        path.chmod(0o444)
    runtime = {
        "image": image,
        "pull_policy": "never",
        "user": "10001:10001",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],  # noqa: S108 - private container tmpfs.
        "pids_limit": 64,
        "mem_limit": "256m",
        "cpus": 1,
        "init": True,
        "restart": "no",
    }
    resolver = f"{directory / 'resolv.conf'}:/etc/resolv.conf:ro"

    def shared(command: list[str], *, mounts: list[str] | None = None) -> dict[str, Any]:
        return {
            **runtime,
            "command": command,
            "network_mode": "service:policy",
            "volumes": [resolver, *(mounts or [])],
        }

    def worker(queue: str, role: str, password: str, address: str) -> dict[str, Any]:
        return {
            **runtime,
            "command": [
                "primary-signal-processor",
                "--queue",
                queue,
                "--once",
                "--retriever-origin",
                FIXTURE_RPC_ORIGIN,
                "--allow-fixture-rpc",
            ],
            "environment": _db_environment(role, password),
            "volumes": [resolver],
            "networks": {
                NETWORKS[0]: {"ipv4_address": f"192.0.2.{address}"},
                NETWORKS[1]: {"ipv4_address": f"198.51.100.{address}"},
            },
        }

    model: dict[str, Any] = {
        "name": project,
        "services": {
            "database": {
                "image": "postgres:18.6",
                "environment": {
                    "POSTGRES_DB": DATABASE,
                    "POSTGRES_USER": BOOTSTRAP_ROLE,
                    "POSTGRES_PASSWORD_FILE": "/run/secrets/database_password",
                },
                "secrets": ["database_password"],
                "networks": {NETWORKS[1]: {"ipv4_address": "198.51.100.10"}},
                "volumes": [
                    f"{VOLUME}:/var/lib/postgresql",
                    *[
                        f"{root / 'deploy/postgres/initdb' / name}:/docker-entrypoint-initdb.d/{name}:ro"
                        for name in INIT_FILES
                    ],
                    f"{directory / 'bootstrap-logins.sql'}:/docker-entrypoint-initdb.d/900_local_logins.sql:ro",
                ],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "pg_isready --host=127.0.0.1 --username=$${POSTGRES_USER} --dbname=$${POSTGRES_DB}",
                    ],
                    "interval": "2s",
                    "timeout": "3s",
                    "retries": 30,
                },
            },
            "setup": {
                **runtime,
                "environment": _db_environment(MIGRATION_ROLE, MIGRATION_PASSWORD),
                "command": ["primary-signal-migrate"],
                "networks": {NETWORKS[1]: {}},
            },
            "policy": {
                **runtime,
                "image": policy_image,
                "user": "0:0",
                "cap_add": ["NET_ADMIN"],
                "command": ["python", "/fixture/policy.py"],
                "networks": {NETWORKS[0]: {"ipv4_address": FIXTURE_RPC_HOST}},
                "volumes": [
                    f"{directory / name}:/fixture/{name}:ro" for name in ("policy.py", "rules.json")
                ],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "from pathlib import Path;assert Path('/tmp/policy-ready').read_text()=='ready'",
                    ],
                    "interval": "1s",
                    "timeout": "2s",
                    "retries": 30,
                },
            },
            "synthetic": shared(
                ["python", "/fixture/server.py"],
                mounts=[f"{directory / 'server.py'}:/fixture/server.py:ro"],
            ),
            "retriever": {
                **shared(
                    [
                        "primary-signal-retriever",
                        "--allow-fixture-rpc",
                        "--host",
                        FIXTURE_RPC_HOST,
                        "--port",
                        "8765",
                    ]
                ),
                "environment": {"PRIMARY_SIGNAL_ENVIRONMENT": "test"},
            },
            "feed_processor": worker("ingestion", FEED_ROLE, FEED_PASSWORD, "11"),
            "article_processor": worker("retrieval", ARTICLE_ROLE, ARTICLE_PASSWORD, "12"),
            "probe": shared(["python", "-c", "import signal;signal.pause()"]),
        },
        "networks": {
            name: {
                "name": f"{project}_{name}",
                "internal": True,
                "ipam": {"config": [{"subnet": subnet}]},
            }
            for name, subnet in zip(NETWORKS, ("192.0.2.0/24", "198.51.100.0/24"), strict=True)
        },
        "volumes": {VOLUME: {"name": f"{project}_{VOLUME}"}},
        "secrets": {
            "database_password": {
                "file": str(directory / "database_password"),
                "name": f"{project}_database_password",
            }
        },
    }
    network_names = NETWORKS
    if transit_example:
        network_names = (*NETWORKS, EGRESS_NETWORK)
        model["networks"][EGRESS_NETWORK] = {
            "name": f"{project}_{EGRESS_NETWORK}",
            "internal": False,
            "ipam": {"config": [{"subnet": "203.0.113.0/24"}]},
        }
        model["services"]["policy"]["networks"][EGRESS_NETWORK] = {
            "ipv4_address": "203.0.113.10",
            "gw_priority": 1,
        }
    path = directory / "compose.synthetic.json"
    path.write_text(json.dumps(model))
    path.chmod(0o600)
    return IsolatedRetrievalFixture(
        root,
        directory,
        project,
        image,
        policy_image,
        docker_args,
        (
            "compose",
            "--project-directory",
            str(root),
            "--project-name",
            project,
            "--env-file",
            "/dev/null",
            "-f",
            str(path),
        ),
        env,
        model,
        files,
        transit_example=transit_example,
        network_names=network_names,
    )


def _normal_model(model: dict[str, Any]) -> dict[str, Any]:
    """Normalize only Compose's documented expansions, retaining all other fields."""
    result = json.loads(json.dumps(model))
    for service in result.get("services", {}).values():
        for default in ("entrypoint", "command"):
            if service.get(default) is None:
                service.pop(default, None)
        health = service.get("healthcheck", {}).get("test")
        if isinstance(health, list):
            service["healthcheck"]["test"] = [
                part.replace("$$", "$") if isinstance(part, str) else part
                for part in cast(list[Any], health)
            ]
        if service.get("mem_limit") in ("256m", "268435456", 268435456):
            service["mem_limit"] = 268435456
        mounts: list[dict[str, Any]] = []
        for mount in service.get("volumes", []):
            if isinstance(mount, str):
                fields = mount.split(":")
                mount = {
                    "type": "bind" if fields[0].startswith("/") else "volume",
                    "source": fields[0],
                    "target": fields[1],
                    **({"read_only": True} if len(fields) == 3 and fields[2] == "ro" else {}),
                }
            if mount.get("bind") in ({}, {"create_host_path": True}):
                mount.pop("bind", None)
            if mount.get("volume") == {}:
                mount.pop("volume")
            mounts.append(mount)
        if mounts:
            service["volumes"] = mounts
        if "secrets" in service:
            service["secrets"] = [
                {"source": s, "target": f"/run/secrets/{s}"} if isinstance(s, str) else s
                for s in service["secrets"]
            ]
        if service.get("network_mode") == "service:policy" and "depends_on" not in service:
            service["depends_on"] = {
                "policy": {"condition": "service_started", "restart": True, "required": True}
            }
    for network in result.get("networks", {}).values():
        if network.get("internal") is False:
            network.pop("internal")
        if network.get("ipam", {}).get("driver") == "default":
            network["ipam"].pop("driver")
    return result


def validate_fixture_models(fixture: IsolatedRetrievalFixture) -> None:
    import subprocess

    # Reject additional includes, files and symlink escapes before Compose can read them.
    path = fixture.directory / "compose.synthetic.json"
    require(
        path.is_file()
        and not path.is_symlink()
        and path.resolve().is_relative_to(fixture.directory),
        "isolated manifest escaped fixture",
    )
    require(
        json.loads(path.read_text()) == fixture.expected_compose, "isolated Compose file changed"
    )
    for filename, content in fixture.expected_files.items():
        leaf = fixture.directory / filename
        require(
            leaf.is_file()
            and not leaf.is_symlink()
            and leaf.resolve().is_relative_to(fixture.directory),
            "isolated fixture file escaped",
        )
        require(leaf.read_text() == content, "isolated fixture file changed")
    root = fixture.root.resolve()
    init = root / "deploy/postgres/initdb"
    require(
        init.resolve().is_relative_to(root) and not init.is_symlink() and init.is_dir(),
        "isolated bootstrap escaped checkout",
    )
    require(
        {p.name for p in init.iterdir()} == set(INIT_FILES), "isolated bootstrap file set changed"
    )
    require(
        all(
            (init / name).is_file()
            and not (init / name).is_symlink()
            and (init / name).resolve().is_relative_to(root)
            for name in INIT_FILES
        ),
        "isolated bootstrap file escaped checkout",
    )
    for flags in (["--no-interpolate", "--no-env-resolution"], []):
        result = subprocess.run(  # noqa: S603 - fixed local Docker/Compose argv only.
            [*fixture.docker_args, *fixture.compose_args, "config", *flags, "--format", "json"],
            env=fixture.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        require(result.returncode == 0, "isolated Compose rendering failed")
        validate_model(fixture, json.loads(result.stdout))


def validate_model(fixture: IsolatedRetrievalFixture, model: dict[str, Any]) -> None:
    require(
        _normal_model(model) == _normal_model(fixture.expected_compose),
        "isolated container boundary changed",
    )
