"""Namespace policy semantics and synthetic-only fixture input boundaries."""

import copy
import json
import socket
import struct
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from scripts import isolated_retrieval_fixture as isolated
from scripts.publication_smoke_fixture import FixtureBoundaryError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path: Path) -> isolated.IsolatedRetrievalFixture:
    return isolated.create_fixture(
        ROOT,
        tmp_path / "fixture",
        "synthetic-isolated",
        image="synthetic-app:local",
        policy_image="synthetic-policy:local",
    )


def _readback() -> dict[str, Any]:
    entries = [
        next(iter(command.values())) for command in json.loads(isolated.rules_source())["nftables"]
    ]
    for entry in entries:
        if "rule" in entry:
            for expression in entry["rule"]["expr"]:
                if "counter" in expression:
                    expression["counter"] = {"packets": 0, "bytes": 0}
    return {"nftables": entries}


def test_policy_is_atomic_owned_table_default_deny_with_no_blanket_loopback() -> None:
    rules = json.loads(isolated.rules_source())["nftables"]
    assert all(set(command) == {"add"} for command in rules)
    counters = isolated.validate_policy_readback(_readback())
    assert {
        "output_denied",
        "output_public",
        "output_drop",
        "output_ipv6",
        "input_drop",
        "forward_drop",
    } <= counters.keys()
    output = [
        entry["add"]["rule"]
        for entry in rules
        if "rule" in entry["add"] and entry["add"]["rule"]["chain"] == "output"
    ]
    assert output[-1]["expr"][-1] == {"drop": None}
    names = [rule["comment"] for rule in output]
    assert names.index("output_denied") < names.index("output_public")
    assert not any(
        rule["expr"]
        == [
            {"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "lo"}},
            {"accept": None},
        ]
        for rule in output
    )


@pytest.mark.parametrize(
    "change",
    [
        "default-accept",
        "missing-deny",
        "accept-forbidden",
        "foreign-table",
        "extra-rule",
        "counter-bool",
    ],
)
def test_policy_readback_rejects_weakened_or_foreign_policy(change: str) -> None:
    value = _readback()
    entries = value["nftables"]
    if change == "default-accept":
        next(e["chain"] for e in entries if "chain" in e)["policy"] = "accept"
    elif change == "missing-deny":
        next(e["set"] for e in entries if "set" in e)["elem"].pop()
    elif change == "foreign-table":
        entries[0]["table"]["name"] = "foreign"
    else:
        rule = next(
            e["rule"] for e in entries if "rule" in e and e["rule"]["comment"] == "output_denied"
        )
        if change == "accept-forbidden":
            rule["expr"][-1] = {"accept": None}
        elif change == "counter-bool":
            rule["expr"][-2]["counter"]["packets"] = True
        else:
            entries.append({"rule": copy.deepcopy(rule)})
    with pytest.raises(FixtureBoundaryError):
        isolated.validate_policy_readback(value)


def test_model_keeps_processors_and_database_out_of_retriever_namespace(
    fixture: isolated.IsolatedRetrievalFixture,
) -> None:
    isolated.validate_model(fixture, fixture.expected_compose)
    services = fixture.expected_compose["services"]
    assert services["policy"]["cap_add"] == ["NET_ADMIN"]
    assert services["policy"]["cap_drop"] == ["ALL"]
    for name in ("synthetic", "retriever", "probe"):
        assert services[name]["network_mode"] == "service:policy"
        assert services[name]["cap_drop"] == ["ALL"]
        assert not any("DATABASE" in key for key in services[name].get("environment", {}))
    for name in ("feed_processor", "article_processor"):
        assert "network_mode" not in services[name]
        assert set(services[name]["networks"]) == set(isolated.NETWORKS)
        assert services[name]["cap_drop"] == ["ALL"]
    assert set(services["policy"]["networks"]) == {isolated.NETWORKS[0]}
    assert all(net["internal"] for net in fixture.expected_compose["networks"].values())
    assert all(not service.get("ports") for service in services.values())


@pytest.mark.parametrize(
    ("service", "field", "value"),
    [
        ("retriever", "cap_add", ["NET_ADMIN"]),
        ("policy", "cap_add", ["SYS_ADMIN"]),
        ("synthetic", "environment", {"PRIMARY_SIGNAL_DATABASE_URL": "synthetic-rejected"}),
        ("retriever", "network_mode", "host"),
        ("probe", "privileged", True),
        ("feed_processor", "network_mode", "service:policy"),
        ("retriever", "volumes", ["/:/operator:ro"]),
        ("retriever", "ports", ["8765:8765"]),
    ],
)
def test_model_rejects_privileges_or_boundary_expansion(
    fixture: isolated.IsolatedRetrievalFixture, service: str, field: str, value: object
) -> None:
    model = copy.deepcopy(fixture.expected_compose)
    model["services"][service][field] = value
    with pytest.raises(FixtureBoundaryError):
        isolated.validate_model(fixture, model)


@pytest.mark.parametrize(
    "filename", ["server.py", "policy.py", "rules.json", "resolv.conf", "bootstrap-logins.sql"]
)
def test_changed_generated_files_fail_before_compose_reads(
    fixture: isolated.IsolatedRetrievalFixture, filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fixture.directory / filename
    path.chmod(0o600)
    path.write_text("synthetic-changed")
    execute = MagicMock(side_effect=AssertionError("unexpected Docker invocation"))
    monkeypatch.setattr("subprocess.run", execute)
    with pytest.raises(FixtureBoundaryError, match="fixture file changed"):
        isolated.validate_fixture_models(fixture)
    execute.assert_not_called()


def _server() -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "synthetic_test_only"}
    exec(compile(isolated.server_source(), "synthetic_source.py", "exec"), namespace)  # noqa: S102 - generated fixed test code, not input.
    return namespace


def _question(host: str, kind: int = 1) -> bytes:
    return (
        struct.pack("!6H", 7, 0x100, 1, 0, 0, 0)
        + b"".join(bytes([len(part)]) + part.encode() for part in host.split("."))
        + b"\0"
        + struct.pack("!HH", kind, 1)
    )


def test_synthetic_dns_returns_only_fixed_answers_without_forwarding() -> None:
    namespace = _server()
    answer = namespace["answer"]
    response = answer(_question(isolated.PUBLIC_HOST))
    assert response[-4:] == bytes([8, 8, 8, 8])
    assert struct.unpack("!6H", response[:12])[3] == 1
    assert struct.unpack("!6H", answer(_question(isolated.MIXED_HOST))[:12])[3] == 2
    assert struct.unpack("!6H", answer(_question(isolated.PUBLIC_HOST, 28))[:12])[3] == 0
    assert struct.unpack("!6H", answer(_question("unknown.fixture.example.test"))[:12])[1] & 15 == 3
    assert namespace["STATE"]["dns_queries"] == 4
    server = isolated.server_source()
    for private in (
        isolated.BOOTSTRAP_PASSWORD,
        isolated.MIGRATION_PASSWORD,
        isolated.FEED_PASSWORD,
        isolated.ARTICLE_PASSWORD,
        "PRIMARY_SIGNAL_DATABASE",
        "getaddrinfo",
        "urlopen",
    ):
        assert private not in server


@pytest.mark.parametrize(
    "packet", [b"", b"x" * 513, b"x" * 12, struct.pack("!6H", 7, 0x100, 2, 0, 0, 0)]
)
def test_synthetic_dns_rejects_invalid_or_unbounded_queries(packet: bytes) -> None:
    namespace = _server()
    assert namespace["answer"](packet) is None
    assert namespace["STATE"]["dns_queries"] == 0


def test_source_port_spoof_cannot_use_control_reply_exemption() -> None:
    value = _readback()
    rule = next(
        entry["rule"]
        for entry in value["nftables"]
        if "rule" in entry and entry["rule"]["comment"] == "output_control_reply"
    )
    established = {"match": {"op": "==", "left": {"ct": {"key": "state"}}, "right": "established"}}
    assert established in rule["expr"]
    rule["expr"].remove(established)
    with pytest.raises(FixtureBoundaryError, match="policy rules changed"):
        isolated.validate_policy_readback(value)


def test_policy_readback_accepts_equivalent_collapsed_networks() -> None:
    value = _readback()
    deny = next(entry["set"] for entry in value["nftables"] if "set" in entry)
    deny["elem"] = [
        prefix
        for prefix in deny["elem"]
        if prefix["prefix"]["addr"] not in ("224.0.0.0", "240.0.0.0")
    ]
    deny["elem"].append({"prefix": {"addr": "224.0.0.0", "len": 3}})
    assert isolated.validate_policy_readback(value)["output_drop"] == 0


def test_manifest_include_fails_before_compose_reads_operator_files(
    fixture: isolated.IsolatedRetrievalFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fixture.directory / "compose.synthetic.json"
    manifest = json.loads(path.read_text())
    manifest["include"] = ["/synthetic-operator.yaml"]
    path.write_text(json.dumps(manifest))
    execute = MagicMock(side_effect=AssertionError("unexpected Compose invocation"))
    monkeypatch.setattr("subprocess.run", execute)
    with pytest.raises(FixtureBoundaryError, match="Compose file changed"):
        isolated.validate_fixture_models(fixture)
    execute.assert_not_called()


def test_bootstrap_ancestor_symlink_fails_before_mount_or_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "synthetic-checkout"
    outside = tmp_path / "synthetic-outside"
    root.mkdir()
    init = outside / "postgres/initdb"
    init.mkdir(parents=True)
    for filename in isolated.INIT_FILES:
        (init / filename).write_text("-- synthetic outside fixture")
    (root / "deploy").symlink_to(outside, target_is_directory=True)
    fixture = isolated.create_fixture(
        root,
        tmp_path / "fixture",
        "synthetic-isolated",
        image="synthetic-app:local",
        policy_image="synthetic-policy:local",
    )
    execute = MagicMock(side_effect=AssertionError("unexpected Compose invocation"))
    monkeypatch.setattr("subprocess.run", execute)
    with pytest.raises(FixtureBoundaryError, match="bootstrap escaped checkout"):
        isolated.validate_fixture_models(fixture)
    execute.assert_not_called()


def test_owned_synthetic_tcp_listener_can_rebind_after_connection_closes() -> None:
    namespace = _server()
    server_type = namespace["FixtureTCPServer"]
    assert server_type.allow_reuse_address is True
    assert server_type.allow_reuse_port is False
    server = server_type(("127.0.0.1", 0), namespace["Forbidden"])
    address = server.server_address
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        with socket.create_connection(address, timeout=2) as connection:
            connection.settimeout(2)
            assert connection.recv(1) == b""
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert namespace["STATE"]["forbidden_requests"] == 1
    finally:
        server.server_close()
        thread.join(timeout=2)
    with server_type(address, namespace["Forbidden"]) as rebound:
        assert rebound.server_address == address
