# pyright: reportPrivateUsage=false
"""The synthetic boundary smoke must prove locality, policy and atomic results."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from scripts import smoke_isolated_retrieval as smoke
from scripts.isolated_retrieval_fixture import IsolatedRetrievalFixture

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
IDENTIFIER = "1" * 64


def fixture(tmp_path: Path) -> IsolatedRetrievalFixture:
    return cast(
        IsolatedRetrievalFixture,
        SimpleNamespace(
            root=tmp_path,
            directory=tmp_path,
            project="synthetic-boundary",
            image="synthetic:local",
            policy_image="synthetic:policy",
            env={"PATH": os.defpath},
            docker_args=("docker",),
            compose_args=("compose",),
            transit_example=False,
            live_three_sources=False,
            network_names=smoke.NETWORKS,
            database_name="synthetic_boundary_test",
            migration_role="synthetic_migration_test",
        ),
    )


def state(service: str = "policy") -> dict[str, Any]:
    return {
        "Id": IDENTIFIER,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "synthetic-boundary",
                "com.docker.compose.service": service,
                "com.docker.compose.oneoff": "False",
            },
            "Env": [],
        },
        "HostConfig": {
            "Privileged": False,
            "PidMode": "",
            "NetworkMode": "synthetic-boundary_retrieval_rpc",
            "Devices": [],
            "RestartPolicy": {"Name": "no"},
            "CapDrop": ["ALL"],
            "CapAdd": ["NET_ADMIN"] if service == "policy" else [],
        },
        "State": {"Status": "running", "StartedAt": "first", "ExitCode": 0},
        "NetworkSettings": {"Ports": {}, "Networks": {"synthetic-boundary_retrieval_rpc": {}}},
        "Mounts": [],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("Privileged", True),
        ("PidMode", "host"),
        ("NetworkMode", "host"),
        ("Devices", [{"PathOnHost": "/dev/example"}]),
        ("RestartPolicy", {"Name": "always"}),
        ("CapAdd", ["NET_ADMIN", "SYS_ADMIN"]),
        ("CapDrop", []),
    ],
)
def test_container_boundary_rejects_host_privilege(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: Any
) -> None:
    data = state()
    data["HostConfig"][field] = value

    def mock_1(*_a: Any, **_k: Any) -> Any:
        return json.dumps(data)

    monkeypatch.setattr(smoke, "_docker", cast(Callable[..., Any], mock_1))
    with pytest.raises(smoke.SmokeFailure):
        smoke._inspect(fixture(tmp_path), "policy", IDENTIFIER)


@pytest.mark.parametrize("change", ["foreign", "oneoff", "published", "wrong_id", "wrong_service"])
def test_exact_owned_identity_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    data = state()
    if change == "foreign":
        data["Config"]["Labels"]["com.docker.compose.project"] = "other"
    elif change == "oneoff":
        data["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
    elif change == "published":
        data["NetworkSettings"]["Ports"] = {"80/tcp": [{"HostPort": "80"}]}
    elif change == "wrong_id":
        data["Id"] = "2" * 64
    else:
        data["Config"]["Labels"]["com.docker.compose.service"] = "retriever"

    def mock_2(*_a: Any, **_k: Any) -> Any:
        return json.dumps(data)

    monkeypatch.setattr(smoke, "_docker", cast(Callable[..., Any], mock_2))
    with pytest.raises(smoke.SmokeFailure):
        smoke._inspect(fixture(tmp_path), "policy", IDENTIFIER)


def test_unapproved_destination_is_rejected_before_any_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    def mock_3(*_a: Any, **_k: Any) -> Any:
        return pytest.fail("destination checked too late")

    monkeypatch.setattr(smoke, "_inspect", cast(Callable[..., Any], mock_3))
    with pytest.raises(smoke.SmokeFailure, match="allowlist"):
        smoke._local_route(fixture(tmp_path), "203.0.113.99")


@pytest.mark.parametrize(
    "route",
    [
        [{"type": "unicast", "dev": "eth0"}],
        [{"type": "local", "dev": "eth0"}],
        [],
        [{"type": "local", "dev": "lo"}, {"type": "local", "dev": "lo"}],
    ],
)
def test_alias_without_local_loopback_route_never_connects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: list[dict[str, str]]
) -> None:

    def namespace(*_args: Any, **_kwargs: Any) -> str:
        return "net:[123]"

    monkeypatch.setattr(smoke, "_namespace", namespace)

    def mock_4(*_a: Any, **_k: Any) -> Any:
        return state()

    monkeypatch.setattr(smoke, "_inspect", cast(Callable[..., Any], mock_4))

    def docker(_f: Any, *args: str, **_kw: Any) -> str:
        return json.dumps(
            [{"addr_info": [{"local": smoke.PUBLIC_IP, "prefixlen": 32}]}]
            if "address" in args
            else route
        )

    monkeypatch.setattr(smoke, "_docker", docker)

    def mock_5(*_a: Any, **_k: Any) -> Any:
        return pytest.fail("unowned route reached connection probe")

    monkeypatch.setattr(smoke, "_python", cast(Callable[..., Any], mock_5))
    with pytest.raises(smoke.SmokeFailure, match="local route"):
        smoke._raw(
            fixture(tmp_path),
            smoke.PUBLIC_IP,
            80,
            kind="http",
            counter="output_public",
            allowed=True,
            receipt="source_requests",
        )


def test_wrong_alias_prefix_is_rejected_before_route_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    def namespace(*_args: Any, **_kwargs: Any) -> str:
        return "net:[123]"

    monkeypatch.setattr(smoke, "_namespace", namespace)

    def mock_6(*_a: Any, **_k: Any) -> Any:
        return state()

    monkeypatch.setattr(smoke, "_inspect", cast(Callable[..., Any], mock_6))

    def docker(_f: Any, *args: str, **_kw: Any) -> str:
        assert "address" in args
        return json.dumps([{"addr_info": [{"local": smoke.PUBLIC_IP, "prefixlen": 24}]}])

    monkeypatch.setattr(smoke, "_docker", docker)
    with pytest.raises(smoke.SmokeFailure, match="owned alias"):
        smoke._local_route(fixture(tmp_path), smoke.PUBLIC_IP)


def receipts(**changes: int) -> dict[str, int]:
    return {
        "dns_queries": 0,
        "source_requests": 0,
        "forbidden_requests": 0,
        "source_443_connections": 0,
    } | changes


@pytest.mark.parametrize(
    "counter_after,receipt_after,allowed,receipt",
    [
        (0, receipts(), False, None),
        (1, receipts(forbidden_requests=1), False, None),
        (1, receipts(), True, "source_requests"),
    ],
)
def test_timeout_or_counter_alone_cannot_prove_policy(
    counter_after: int, receipt_after: dict[str, int], allowed: bool, receipt: str | None
) -> None:
    with pytest.raises(smoke.SmokeFailure):
        smoke._evidence(
            {"rule": 0},
            {"rule": counter_after},
            receipts(),
            receipt_after,
            counter="rule",
            allowed=allowed,
            receipt=receipt,
        )


def test_positive_and_negative_evidence_requires_both_dimensions() -> None:
    smoke._evidence(
        {"rule": 0},
        {"rule": 1},
        receipts(),
        receipts(source_requests=1),
        counter="rule",
        allowed=True,
        receipt="source_requests",
    )
    smoke._evidence({"rule": 0}, {"rule": 1}, receipts(), receipts(), counter="rule", allowed=False)


@pytest.mark.parametrize(
    "bad", [{"dns_queries": True}, receipts(forbidden_requests=-1), receipts() | {"unexpected": 0}]
)
def test_invalid_sink_receipts_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: dict[str, Any]
) -> None:

    def mock_7(*_a: Any, **_k: Any) -> Any:
        return bad

    monkeypatch.setattr(smoke, "_python", cast(Callable[..., Any], mock_7))
    with pytest.raises(smoke.SmokeFailure, match="receipt shape"):
        smoke._state(fixture(tmp_path))


def snapshot(complete: bool) -> dict[str, Any]:
    return {
        "jobs": [
            ["articles.retrieve", "succeeded" if complete else "queued", 1 if complete else 0],
            ["feeds.poll", "succeeded", 1],
        ],
        "polls": ["succeeded"],
        "lineage": True,
        "successor_linked": True,
        "attempts": ["succeeded"] * (2 if complete else 1),
        "versions": 1 if complete else 0,
        "fetches": ["fetched"] if complete else [],
        "current_matches": complete,
        "search_matches": complete,
        "search_count": 1 if complete else 0,
    }


@pytest.mark.parametrize("complete", [False, True])
def test_atomic_pipeline_snapshots_accept_correct_results(complete: bool) -> None:
    smoke._pipeline_snapshot(snapshot(complete), complete=complete)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lineage", False),
        ("successor_linked", False),
        ("attempts", ["succeeded", "succeeded", "succeeded"]),
        ("versions", 2),
        ("current_matches", False),
        ("search_matches", False),
        ("search_count", 2),
        ("fetches", ["success", "success"]),
    ],
)
def test_duplicate_or_unlinked_results_cannot_pass(field: str, value: Any) -> None:
    data = snapshot(True)
    data[field] = value
    with pytest.raises(smoke.SmokeFailure):
        smoke._pipeline_snapshot(data, complete=True)


def test_feed_phase_cannot_already_contain_fetched_text() -> None:
    data = snapshot(False)
    data["versions"] = 1
    with pytest.raises(smoke.SmokeFailure, match="content separation"):
        smoke._pipeline_snapshot(data, complete=False)


@pytest.mark.parametrize("change", ["timestamp", "stopped"])
def test_owner_changes_abort_before_further_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    owner = state()
    current = state()
    current["State"]["StartedAt" if change == "timestamp" else "Status"] = "changed"

    def mock_8(*_a: Any, **_k: Any) -> Any:
        return current

    monkeypatch.setattr(smoke, "_inspect", cast(Callable[..., Any], mock_8))

    def mock_9(*_a: Any) -> Any:
        return pytest.fail("changed owner accepted")

    monkeypatch.setattr(smoke, "_policy", cast(Callable[..., Any], mock_9))
    with pytest.raises(smoke.SmokeFailure, match="owner lifetime"):
        smoke._owner_unchanged(fixture(tmp_path), owner)


def test_kernel_readback_failure_prevents_retriever_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = fixture(tmp_path)
    f.directory.mkdir(exist_ok=True)
    calls: list[tuple[str, ...]] = []

    def mock_10(*_a: Any) -> Any:
        return tmp_path

    monkeypatch.setattr(smoke.publication, "_build_context", cast(Callable[..., Any], mock_10))

    def docker(_f: Any, *args: str, **_kw: Any) -> str:
        return "sha256:" + "2" * 64 if args[:2] == ("image", "inspect") else ""

    monkeypatch.setattr(smoke, "_docker", docker)

    def mock_11(_f: Any, *a: Any, **_k: Any) -> Any:
        return calls.append(a) or ""

    monkeypatch.setattr(smoke, "_compose", cast(Callable[..., Any], mock_11))

    def mock_12(*_a: Any) -> Any:
        return (_ for _ in ()).throw(smoke.SmokeFailure("readback mismatch"))

    monkeypatch.setattr(smoke, "_policy", cast(Callable[..., Any], mock_12))
    object.__setattr__(f, "expected_files", {"Dockerfile.policy": "FROM scratch\n"})
    with pytest.raises(smoke.SmokeFailure, match="readback mismatch"):
        smoke._exercise(f)
    assert len(calls) == 1 and calls[0][-2:] == ("database", "policy")


def test_managed_restart_refuses_to_restart_owner_with_running_dependents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def mock_13(_f: Any, service: Any, *_a: Any) -> Any:
        return state(service)

    monkeypatch.setattr(smoke, "_inspect", cast(Callable[..., Any], mock_13))

    def mock_14(*_a: Any) -> Any:
        return None

    monkeypatch.setattr(smoke, "_owner_unchanged", cast(Callable[..., Any], mock_14))

    def mock_15(_f: Any, *a: Any, **_k: Any) -> Any:
        return calls.append(a) or ""

    monkeypatch.setattr(smoke, "_compose", cast(Callable[..., Any], mock_15))

    def mock_16(*_a: Any, **_k: Any) -> Any:
        return pytest.fail("owner restarted before dependent stop verified")

    monkeypatch.setattr(smoke, "_docker", cast(Callable[..., Any], mock_16))
    with pytest.raises(smoke.SmokeFailure, match="workload stop"):
        smoke._managed_restart(fixture(tmp_path), state())
    assert calls[0][0] == "stop"


def test_cleanup_attempts_both_owned_images_after_compose_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    removed: list[str] = []

    def mock_17(*_a: Any, **_k: Any) -> Any:
        return (_ for _ in ()).throw(smoke.SmokeFailure("cleanup"))

    monkeypatch.setattr(smoke, "_compose", cast(Callable[..., Any], mock_17))

    def mock_18(_f: Any, kind: Any, name: Any) -> Any:
        return kind == "image" and name not in removed

    monkeypatch.setattr(smoke, "_exists", cast(Callable[..., Any], mock_18))

    def docker(_f: Any, *args: str, **_kw: Any) -> str:
        if args[:2] == ("image", "rm"):
            removed.append(args[2])
        return ""

    monkeypatch.setattr(smoke, "_docker", docker)
    with pytest.raises(smoke.SmokeFailure, match="cleanup"):
        smoke._cleanup(fixture(tmp_path))
    assert removed == ["synthetic:local", "synthetic:policy"]


def test_probe_must_share_current_owner_namespace_before_alias_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def namespace(_fixture: Any, service: str) -> str:
        return "net:[123]" if service == "policy" else "net:[456]"

    def inspect(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("alias inspection ran before probe namespace match")

    monkeypatch.setattr(smoke, "_namespace", namespace)
    monkeypatch.setattr(smoke, "_inspect", inspect)
    with pytest.raises(smoke.SmokeFailure, match="probe namespace identity"):
        smoke._local_route(fixture(tmp_path), smoke.PUBLIC_IP)


def test_absence_inspection_does_not_hide_daemon_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def command(*_args: Any, **_kwargs: Any) -> Any:
        import subprocess

        return subprocess.CompletedProcess([], 1, "", "permission denied")

    monkeypatch.setattr(smoke.publication, "_command", command)
    with pytest.raises(smoke.SmokeFailure, match="resource inspection"):
        smoke._exists(fixture(tmp_path), "image", "synthetic:local")


def test_null_unpublished_ports_are_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = state()
    data["NetworkSettings"]["Ports"] = None

    def docker(*_args: Any, **_kwargs: Any) -> str:
        return json.dumps(data)

    monkeypatch.setattr(smoke, "_docker", docker)
    assert smoke._inspect(fixture(tmp_path), "policy", IDENTIFIER)["Id"] == IDENTIFIER


def test_embedded_processor_and_probe_programs_compile() -> None:
    for name in ("DATABASE_PROBE", "RAW_PROBE", "RPC_PROBE"):
        compile(getattr(smoke, name), name, "exec")


def test_readiness_runs_in_processor_namespace_without_claiming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[tuple[str, ...]] = []

    def compose(_fixture: Any, *args: str, **_kwargs: Any) -> str:
        captured.append(args)
        compile(args[-1], "readiness", "exec")
        return ""

    monkeypatch.setattr(smoke, "_compose", compose)
    smoke._ready(fixture(tmp_path))
    assert captured[0][0:6] == ("run", "--rm", "--no-deps", "-T", "feed_processor", "python")
    assert "require_ready" in captured[0][-1]
    assert "claim" not in captured[0][-1]


@pytest.mark.parametrize("capability", ["NET_ADMIN", "CAP_NET_ADMIN"])
def test_docker_canonical_capability_names_preserve_exact_capability_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capability: str
) -> None:
    data = state()
    data["HostConfig"]["CapAdd"] = [capability]

    def docker(*_args: Any, **_kwargs: Any) -> str:
        return json.dumps(data)

    monkeypatch.setattr(smoke, "_docker", docker)
    assert smoke._inspect(fixture(tmp_path), "policy", IDENTIFIER)["Id"] == IDENTIFIER


def test_exact_three_rejection_targets_are_urls_not_resolver_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    targets: list[str] = []

    def compose(_fixture: Any, *_args: Any, **kwargs: Any) -> str:
        targets.append(json.loads(kwargs["input_text"])["url"])
        return json.dumps({"status": 502, "error": "forbidden_address"})

    def control(*_args: Any) -> dict[str, int]:
        return receipts()

    monkeypatch.setattr(smoke, "_compose", compose)
    monkeypatch.setattr(smoke, "_state", control)
    smoke._rejected_targets(fixture(tmp_path))
    assert targets == [
        "http://" + smoke.PRIVATE_HOST + "/article",
        "http://" + smoke.MIXED_HOST + "/article",
        smoke.REDIRECT_URL,
    ]


@pytest.mark.parametrize(
    "result", [{"status": 200, "error": None}, {"status": 502, "error": "dns_error"}]
)
def test_rejection_requires_specific_policy_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: dict[str, Any]
) -> None:
    def compose(*_args: Any, **_kwargs: Any) -> str:
        return json.dumps(result)

    monkeypatch.setattr(smoke, "_compose", compose)
    with pytest.raises(smoke.SmokeFailure, match="private rejection"):
        smoke._rejected_targets(fixture(tmp_path))


def transit_fixture(tmp_path: Path) -> IsolatedRetrievalFixture:
    value = fixture(tmp_path)
    object.__setattr__(value, "transit_example", True)
    object.__setattr__(value, "network_names", (*smoke.NETWORKS, "retrieval_egress"))
    return value


def test_transit_validates_all_aaaa_before_selecting_ipv4() -> None:
    from primary_signal.retrieval.policy import PolicyError

    with pytest.raises(PolicyError, match="forbidden_address"):
        smoke._transit_answers({"addresses": ["9.9.9.9", "fd00::1"]})
    assert smoke._transit_answers({"addresses": ["9.9.9.9", "2001:4860:4860::8888"]}) == (
        "9.9.9.9",
    )


@pytest.mark.parametrize(
    "answers", [[], ["8.8.8.8"], ["127.0.0.1"], [True], ["2001:4860:4860::8888"]]
)
def test_transit_rejects_local_alias_invalid_or_ipv6_only_candidates(answers: list[Any]) -> None:
    with pytest.raises((smoke.SmokeFailure, ValueError)):
        smoke._transit_answers({"addresses": answers})


def transit_metadata() -> dict[str, Any]:
    return {
        "api_status": 200,
        "source_status": 200,
        "exact_url": True,
        "empty_redirect_chain": True,
        "bounded": True,
        "html": True,
        "title_matches": True,
        "hashes": True,
        "word_count": 42,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_status", 502),
        ("source_status", 304),
        ("exact_url", False),
        ("empty_redirect_chain", False),
        ("bounded", False),
        ("html", False),
        ("title_matches", False),
        ("hashes", False),
        ("word_count", True),
        ("word_count", 0),
        ("word_count", 4097),
    ],
)
def test_real_https_requires_bounded_exact_nonredirected_response(field: str, value: Any) -> None:
    result = transit_metadata()
    result[field] = value
    with pytest.raises(smoke.SmokeFailure, match="verified example HTTPS"):
        smoke._transit_result(result)


def test_real_https_metadata_accepts_only_verified_shape() -> None:
    smoke._transit_result(transit_metadata())
    with pytest.raises(smoke.SmokeFailure):
        smoke._transit_result(transit_metadata() | {"unexpected": "body"})


@pytest.mark.parametrize("key", sorted(smoke.UPSTREAM_KEYS))
def test_external_dns_cannot_precede_synthetic_restart_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str
) -> None:
    def control(*_args: Any) -> dict[str, int]:
        return receipts() | dict.fromkeys(smoke.UPSTREAM_KEYS, 0) | {key: 1}

    monkeypatch.setattr(smoke, "_state", control)
    with pytest.raises(smoke.SmokeFailure, match="before transit phase"):
        smoke._no_upstream(transit_fixture(tmp_path))


def test_default_profile_never_checks_an_upstream_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def control(*_args: Any) -> Any:
        pytest.fail("default synthetic proof attempted upstream access")

    monkeypatch.setattr(smoke, "_state", control)
    smoke._no_upstream(fixture(tmp_path))


@pytest.mark.parametrize(
    "bad",
    [
        {"rpc": "lo", "egress": "eth1"},
        {"rpc": "eth0", "egress": "eth0"},
        {"rpc": "eth0", "egress": None},
        {"rpc": "eth0"},
    ],
)
def test_transit_interface_receipt_must_name_distinct_nonloopback_interfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: dict[str, Any]
) -> None:
    def inspect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return state()

    def python(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return bad

    def docker(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid interface receipt reached address probing")

    monkeypatch.setattr(smoke, "_inspect", inspect)
    monkeypatch.setattr(smoke, "_python", python)
    monkeypatch.setattr(smoke, "_docker", docker)
    with pytest.raises(smoke.SmokeFailure, match="interface names"):
        smoke._interfaces(transit_fixture(tmp_path))


@pytest.mark.parametrize(
    "change",
    [
        {"type": "local"},
        {"dev": "lo"},
        {"dev": "eth0"},
        {"prefsrc": "203.0.113.12"},
        {"gateway": "203.0.113.2"},
        {"dst": "9.9.9.9"},
    ],
)
def test_external_route_must_use_owned_egress_gateway_and_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, str]
) -> None:
    interfaces = {"rpc": "eth0", "egress": "eth1"}
    owner = state()
    owner["NetworkSettings"]["Networks"]["synthetic-boundary_retrieval_egress"] = {
        "IPAddress": "203.0.113.10",
        "Gateway": "203.0.113.1",
    }
    route = {
        "type": "unicast",
        "dev": "eth1",
        "prefsrc": "203.0.113.10",
        "gateway": "203.0.113.1",
        "dst": smoke.UPSTREAM_DNS_IP,
    } | change

    def namespace(*_args: Any) -> str:
        return "net:[123]"

    def observed_interfaces(*_args: Any) -> dict[str, str]:
        return interfaces

    def inspect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return owner

    def docker(*_args: Any, **_kwargs: Any) -> str:
        return json.dumps([route])

    monkeypatch.setattr(smoke, "_namespace", namespace)
    monkeypatch.setattr(smoke, "_interfaces", observed_interfaces)
    monkeypatch.setattr(smoke, "_inspect", inspect)
    monkeypatch.setattr(smoke, "_docker", docker)
    with pytest.raises(smoke.SmokeFailure, match="owned external route"):
        smoke._external_route(transit_fixture(tmp_path), smoke.UPSTREAM_DNS_IP, interfaces)


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.169.254", "::1"])
def test_external_probe_never_uses_alias_or_special_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, address: str
) -> None:
    def namespace(*_args: Any) -> Any:
        pytest.fail("special destination reached namespace probing")

    monkeypatch.setattr(smoke, "_namespace", namespace)
    with pytest.raises((smoke.SmokeFailure, ValueError)):
        smoke._external_route(transit_fixture(tmp_path), address, {"rpc": "eth0", "egress": "eth1"})


def test_failed_public_dns_stops_before_https_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def noop(*_args: Any, **_kwargs: Any) -> Any:
        return None

    def topology(*_args: Any) -> dict[str, Any]:
        return state()

    def interfaces(*_args: Any) -> dict[str, str]:
        return {"rpc": "eth0", "egress": "eth1"}

    def counters(*_args: Any) -> dict[str, int]:
        return {"output_upstream_dns_udp": 0, "input_upstream_dns_reply_udp": 0}

    def control(*_args: Any) -> dict[str, int]:
        return receipts() | dict.fromkeys(smoke.UPSTREAM_KEYS, 0)

    def python(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"response": False, "type": 1, "answers": 0}

    def compose(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("HTTPS invoked after DNS failed")

    for name, callback in [
        ("_topology", topology),
        ("_no_upstream", noop),
        ("_interfaces", interfaces),
        ("_external_route", noop),
        ("_local_route", noop),
        ("_policy", counters),
        ("_state", control),
        ("_python", python),
        ("_compose", compose),
    ]:
        monkeypatch.setattr(smoke, name, callback)
    with pytest.raises(smoke.SmokeFailure, match="approved public DNS udp"):
        smoke._transit(transit_fixture(tmp_path))


def test_transit_runs_four_dns_queries_then_one_unchanged_api_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    ticks = {"policy": 0, "state": 0}

    def noop(*_args: Any, **_kwargs: Any) -> Any:
        return None

    def topology(*_args: Any) -> dict[str, Any]:
        return state()

    def interfaces(*_args: Any) -> dict[str, str]:
        return {"rpc": "eth0", "egress": "eth1"}

    def route(_fixture: Any, address: str, *_args: Any) -> None:
        events.append("route:" + address)

    def policy(*_args: Any) -> dict[str, int]:
        ticks["policy"] += 1
        return dict.fromkeys(
            (
                "output_upstream_dns_udp",
                "input_upstream_dns_reply_udp",
                "output_upstream_dns_tcp",
                "input_upstream_dns_reply_tcp",
                "output_public",
                "input_public_reply",
            ),
            ticks["policy"],
        )

    def control(*_args: Any) -> dict[str, int]:
        ticks["state"] += 1
        return receipts() | {
            "upstream_udp_queries": ticks["state"],
            "upstream_tcp_queries": ticks["state"],
            "upstream_failures": 0,
        }

    def python(
        _fixture: Any, _service: str, code: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        if code == smoke.DNS_WIRE_PROBE:
            events.append("dns:" + parameters["protocol"] + ":" + str(parameters["type"]))
            return {"response": True, "type": parameters["type"], "answers": 1}
        assert code == smoke.TRANSIT_ANSWERS
        events.append("all_answers")
        return {"addresses": ["9.9.9.9", "2001:4860:4860::8888"]}

    def compose(_fixture: Any, *args: str, **kwargs: Any) -> str:
        assert args[-1] == smoke.TRANSIT_PROBE
        assert json.loads(kwargs["input_text"]) == {"url": "https://example.com/"}
        events.append("one_https_api")
        return json.dumps(transit_metadata())

    def database(*_args: Any) -> dict[str, Any]:
        return snapshot(True)

    for name, callback in [
        ("_topology", topology),
        ("_no_upstream", noop),
        ("_interfaces", interfaces),
        ("_external_route", route),
        ("_local_route", noop),
        ("_policy", policy),
        ("_state", control),
        ("_python", python),
        ("_compose", compose),
        ("_owner_unchanged", noop),
        ("_database", database),
    ]:
        monkeypatch.setattr(smoke, name, callback)
    smoke._transit(transit_fixture(tmp_path))
    assert [e for e in events if e.startswith("dns:")] == [
        "dns:udp:1",
        "dns:udp:28",
        "dns:tcp:1",
        "dns:tcp:28",
    ]
    assert events.count("one_https_api") == 1
    assert (
        events.index("all_answers") < events.index("route:9.9.9.9") < events.index("one_https_api")
    )


def test_transit_embedded_programs_compile() -> None:
    for name in ("DNS_WIRE_PROBE", "TRANSIT_ANSWERS", "TRANSIT_PROBE"):
        compile(getattr(smoke, name), name, "exec")


def test_upstream_packet_counters_must_be_zero_before_transit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def control(*_args: Any) -> dict[str, int]:
        return receipts() | dict.fromkeys(smoke.UPSTREAM_KEYS, 0)

    def counters(*_args: Any) -> dict[str, int]:
        return {
            "output_upstream_dns_udp": 1,
            "input_upstream_dns_reply_udp": 0,
            "output_upstream_dns_tcp": 0,
            "input_upstream_dns_reply_tcp": 0,
        }

    monkeypatch.setattr(smoke, "_state", control)
    monkeypatch.setattr(smoke, "_policy", counters)
    with pytest.raises(smoke.SmokeFailure, match="upstream counters before transit phase"):
        smoke._no_upstream(transit_fixture(tmp_path))


def test_live_probe_references_only_real_model_attributes() -> None:
    import ast

    from primary_signal.ingestion import models as ingestion
    from primary_signal.jobs import models as jobs
    from primary_signal.sources import models as sources

    models = {
        name: getattr(module, name)
        for module, names in (
            (sources, ("Source", "Feed")),
            (jobs, ("Job", "JobAttempt")),
            (
                ingestion,
                (
                    "Article",
                    "ArticleUrl",
                    "FeedEntry",
                    "FeedPollRun",
                    "FetchAttempt",
                    "ContentVersion",
                ),
            ),
        )
        for name in names
    }
    for node in ast.walk(ast.parse(smoke.LIVE_DATABASE_PROBE)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in models
        ):
            assert hasattr(models[node.value.id], node.attr), (node.value.id, node.attr)
    compile(smoke.LIVE_DATABASE_PROBE, "private live probe", "exec")


def private_live_function(name: str) -> Any:
    import ast

    tree = ast.parse(smoke.LIVE_DATABASE_PROBE)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace: dict[str, Any] = {}
    exec(  # noqa: S102 - execute only one function parsed from our trusted fixture program.
        compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), name, "exec"),
        namespace,
    )
    return namespace[name]


@pytest.mark.parametrize(
    "title,body",
    [
        ("Just a moment...", "Checking your browser"),
        ("Access denied", "A request was blocked"),
        ("Article", "Please enable JavaScript and cookies to continue"),
        (None, "Forbidden"),
        ("Article", "Please sign in to continue"),
        ("Consent required", "Choose consent to continue"),
    ],
)
def test_private_live_challenge_pages_never_pass_as_articles(title: str | None, body: str) -> None:
    assert private_live_function("content_allowed")(title, body) is False


def test_legitimate_security_research_can_discuss_challenges() -> None:
    assert (
        private_live_function("content_allowed")(
            "Testing browser security",
            "This report examines CAPTCHA challenges and access denied responses in depth.",
        )
        is True
    )


def test_challenge_html_is_rejected_after_real_extraction() -> None:
    from primary_signal.retrieval.extract import extract_article_html

    result = extract_article_html(
        b"<html><title>Just a moment...</title><article>Verify you are human before continuing.</article></html>",
        "text/html",
    )
    assert private_live_function("content_allowed")(result.title, result.text) is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("feed_attempts", 4),
        ("article_attempts", 4),
        ("feed_attempts", True),
        ("article_attempts", -1),
    ],
)
def test_live_attempt_totals_are_bounded(key: str, value: Any) -> None:
    values = {"feed_attempts": 3, "article_attempts": 3} | {key: value}
    with pytest.raises(smoke.SmokeFailure, match="attempt bound"):
        smoke._live_counts(values, {})


def mock_live_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fail_mode: str | None = None
) -> tuple[IsolatedRetrievalFixture, dict[str, int], list[str]]:
    f = transit_fixture(tmp_path)
    object.__setattr__(f, "live_three_sources", True)
    counts = {
        "feed_attempts": 0,
        "article_attempts": 0,
        "source_classes_passed": 0,
        "fulltext_search_checks": 0,
        "cleanup_passes": 0,
    }
    actual = {"feed_attempts": 0, "article_attempts": 0}
    events: list[str] = []
    selected = [
        {
            "class_key": source.class_key,
            "job_id": str(__import__("uuid").uuid4()),
            "article_id": str(__import__("uuid").uuid4()),
            "url_id": str(__import__("uuid").uuid4()),
        }
        for source in smoke.LIVE_SOURCES
    ]

    def probe(
        _fixture: Any,
        mode: str,
        sources: list[dict[str, str]],
        _selected: list[dict[str, str]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        events.append(mode)
        assert len(sources) == 3 and len({s["source_id"] for s in sources}) == 3
        if mode == fail_mode:
            raise smoke.SmokeFailure("synthetic controlled failure")
        if mode == "seed":
            return {"seeded": 3}
        if mode.startswith("activate_"):
            return {"activated": 1}
        if mode == "poll":
            return {"poll_passed": 1}
        if mode == "select":
            assert actual == {"feed_attempts": 3, "article_attempts": 0}
            return {"selected": selected}
        if mode == "verify_article":
            return {"article_passed": 1, "search_passed": 1}
        if mode == "complete":
            return {
                "feed_attempts": 3,
                "article_attempts": 3,
                "classes_passed": 3,
                "search_checks": 3,
            }
        assert mode == "counts"
        return actual

    def worker(_fixture: Any, service: str, _owner: Any) -> None:
        events.append(service)
        actual["feed_attempts" if service == "feed_processor" else "article_attempts"] += 1

    def topology(*_args: Any) -> dict[str, Any]:
        return state()

    def unchanged(*_args: Any) -> None:
        return None

    monkeypatch.setattr(smoke, "_live_probe", probe)
    monkeypatch.setattr(smoke, "_worker", worker)
    monkeypatch.setattr(smoke, "_topology", topology)
    monkeypatch.setattr(smoke, "_owner_unchanged", unchanged)
    return f, counts, events


def test_live_selection_is_fixed_after_three_polls_before_three_article_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f, counts, events = mock_live_run(monkeypatch, tmp_path)
    smoke._live_sources(f, counts)
    assert (
        counts["feed_attempts"]
        == counts["article_attempts"]
        == counts["source_classes_passed"]
        == counts["fulltext_search_checks"]
        == 3
    )
    assert events.count("feed_processor") == events.count("article_processor") == 3
    assert events.index("select") > max(
        i for i, event in enumerate(events) if event == "feed_processor"
    )
    assert events.index("select") < events.index("article_processor")
    assert events.count("select") == 1


@pytest.mark.parametrize(
    "failure,feeds,articles", [("poll", 1, 0), ("select", 3, 0), ("verify_article", 3, 1)]
)
def test_live_failure_stops_without_retry_or_substitute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, feeds: int, articles: int
) -> None:
    f, counts, events = mock_live_run(monkeypatch, tmp_path, fail_mode=failure)
    with pytest.raises(smoke.SmokeFailure, match="controlled failure"):
        smoke._live_sources(f, counts)
    assert counts["feed_attempts"] == feeds and counts["article_attempts"] == articles
    assert events.count("select") <= 1 and events[-1] == "counts"
    assert events.count("article_processor") == articles


@pytest.mark.parametrize(
    "failure", [None, "controlled", "unexpected", "cleanup", "cleanup_unexpected"]
)
def test_live_cli_emits_only_aggregate_counts_on_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str | None,
) -> None:
    canary = "PRIVATE-CANARY https://private.example.test/article fixture-id content hash history"
    f = transit_fixture(tmp_path)
    object.__setattr__(f, "live_three_sources", True)

    def create(*_args: Any, **kwargs: Any) -> IsolatedRetrievalFixture:
        assert kwargs["transit_example"] is True and kwargs["live_three_sources"] is True
        return f

    def noop(*_args: Any) -> None:
        return None

    def exercise(_fixture: Any, counts: dict[str, int]) -> None:
        smoke._progress(canary)
        counts.update(
            feed_attempts=3, article_attempts=3, source_classes_passed=3, fulltext_search_checks=3
        )
        if failure == "controlled":
            raise smoke.SmokeFailure(canary)
        if failure == "unexpected":
            raise RuntimeError(canary)

    def cleanup(*_args: Any) -> None:
        smoke._progress(canary)
        if failure == "cleanup":
            raise smoke.SmokeFailure(canary)
        if failure == "cleanup_unexpected":
            raise ValueError(canary)

    monkeypatch.setattr(smoke, "_live_output", False)
    monkeypatch.setattr(smoke, "create_fixture", create)
    monkeypatch.setattr(smoke, "validate_fixture_models", noop)
    monkeypatch.setattr(smoke, "_fresh", noop)
    monkeypatch.setattr(smoke, "_exercise", exercise)
    monkeypatch.setattr(smoke, "_cleanup", cleanup)
    result = smoke.main(["--live-three-sources"])
    output = capsys.readouterr()
    assert result == (0 if failure is None else 1)
    assert canary not in output.out + output.err
    assert len(output.out.splitlines()) == 1
    summary = json.loads(output.out)
    assert set(summary) == {
        "feed_attempts",
        "article_attempts",
        "source_classes_passed",
        "fulltext_search_checks",
        "cleanup_passes",
        *{"failure_" + category for category in smoke.FAILURE_CATEGORIES},
    }
    assert all(type(count) is int for count in summary.values())
    assert sum(summary["failure_" + category] for category in smoke.FAILURE_CATEGORIES) == (
        0 if failure is None else 1
    )
    assert summary["cleanup_passes"] == (0 if failure in ("cleanup", "cleanup_unexpected") else 1)


def test_live_temp_directory_exit_failure_cannot_report_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "PRIVATE exit failure content history"
    f = transit_fixture(tmp_path)
    object.__setattr__(f, "live_three_sources", True)

    class Directory:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args: Any) -> None:
            raise RuntimeError(canary)

    def create(*_args: Any, **_kwargs: Any) -> IsolatedRetrievalFixture:
        return f

    def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(smoke, "_live_output", False)
    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", Directory)
    monkeypatch.setattr(smoke, "create_fixture", create)
    for name in ("validate_fixture_models", "_fresh", "_exercise", "_cleanup"):
        monkeypatch.setattr(smoke, name, noop)
    assert smoke.main(["--live-three-sources"]) == 1
    output = capsys.readouterr()
    assert canary not in output.out + output.err
    assert json.loads(output.out)["cleanup_passes"] == 0


@pytest.mark.parametrize("category", smoke.FAILURE_CATEGORIES)
def test_private_category_response_is_strict_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, category: str
) -> None:
    import subprocess

    def run(*_args: Any, **kwargs: Any) -> Any:
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            [], 1, json.dumps({"failure": category}), "PRIVATE diagnostics are never forwarded"
        )

    monkeypatch.setattr(smoke, "_run", run)
    with pytest.raises(smoke.LiveProofFailure) as failure:
        smoke._live_probe(transit_fixture(tmp_path), "verify_article", [], [])
    assert failure.value.category == category
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize(
    "returncode,payload",
    [
        (0, {"failure": "fetch"}),
        (1, {"failure": "other"}),
        (1, {"failure": "search", "body": "PRIVATE"}),
        (1, {"failure": True}),
        (1, {"unexpected": "PRIVATE"}),
    ],
)
def test_unknown_or_success_marked_failure_is_unexpected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, payload: dict[str, Any]
) -> None:
    import subprocess

    def run(*_args: Any, **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess([], returncode, json.dumps(payload), "PRIVATE")

    monkeypatch.setattr(smoke, "_run", run)
    with pytest.raises(smoke.LiveProofFailure) as failure:
        smoke._live_probe(transit_fixture(tmp_path), "verify_article", [], [])
    assert failure.value.category == "unexpected"
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize(
    "payload", ["not JSON", '{"failure":"fetch"}\n{"failure":"search"}', '["PRIVATE"]']
)
def test_private_failure_parser_never_exposes_invalid_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str
) -> None:
    import subprocess

    def run(*_args: Any, **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess([], 1, payload, "PRIVATE")

    monkeypatch.setattr(smoke, "_run", run)
    with pytest.raises(smoke.LiveProofFailure) as failure:
        smoke._live_probe(transit_fixture(tmp_path), "verify_article", [], [])
    assert failure.value.category == "unexpected"


def test_only_first_failure_category_is_recorded() -> None:
    counts: dict[str, int] = {}
    smoke._record_live_failure(counts, "search")
    smoke._record_live_failure(counts, "unexpected")
    assert counts == {
        "failure_" + category: int(category == "search") for category in smoke.FAILURE_CATEGORIES
    }


@pytest.mark.parametrize("value", [True, -1, 2, "1"])
def test_failure_counts_reject_boolean_and_unbounded_values(value: Any) -> None:
    with pytest.raises(smoke.SmokeFailure, match="aggregate failure counter"):
        smoke._record_live_failure({"failure_fetch": value}, "search")


def test_controlled_failure_survives_final_count_collection_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f, counts, _events = mock_live_run(monkeypatch, tmp_path)
    original = smoke._live_probe

    def probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if args[1] == "verify_article":
            raise smoke.LiveProofFailure("search")
        if args[1] == "counts":
            raise smoke.LiveProofFailure("unexpected")
        return original(*args, **kwargs)

    monkeypatch.setattr(smoke, "_live_probe", probe)
    with pytest.raises(smoke.LiveProofFailure) as failure:
        smoke._live_sources(f, counts)
    assert failure.value.category == "search"
    assert counts["failure_search"] == 1 and counts["failure_unexpected"] == 0


def test_bad_private_mode_emits_only_unexpected_json(tmp_path: Path) -> None:
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603 - execute our fixed private program, never a shell.
        [sys.executable, "-c", smoke.LIVE_DATABASE_PROBE],
        input=json.dumps({"mode": "PRIVATE canary"}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.strip() == json.dumps({"failure": "unexpected"})
    assert result.stderr == ""
