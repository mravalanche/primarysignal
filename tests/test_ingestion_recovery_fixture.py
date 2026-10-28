"""Closed fixture-server behavior and container configuration boundaries."""

import copy
import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from scripts import ingestion_recovery_fixture as recovery
from scripts.publication_smoke_fixture import FixtureBoundaryError

from primary_signal.ingestion.feed_parser import parse_feed
from primary_signal.ingestion.feed_polls import FeedPollTarget
from primary_signal.retrieval.client import RetrieverClient
from primary_signal.retrieval.protocol import ARTICLE_FETCH_CAPABILITY, FEED_FETCH_CAPABILITY

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["article", "feed"])
def fixture(tmp_path: Path, request: pytest.FixtureRequest) -> recovery.IngestionRecoveryFixture:
    return recovery.create_fixture(
        ROOT,
        tmp_path / "fixture",
        "synthetic-ingestion",
        image="synthetic-ingestion:local",
        mode=request.param,
    )


def _model(fixture: recovery.IngestionRecoveryFixture, *, resolved: bool) -> dict[str, Any]:
    model = copy.deepcopy(fixture.expected_compose)
    for name, service in model["services"].items():
        mounts: list[dict[str, Any]] = []
        for value in service.get("volumes", []):
            parts = value.split(":")
            source, target = parts[:2]
            mount: dict[str, Any] = {
                "type": "bind" if source.startswith("/") else "volume",
                "source": source,
                "target": target,
            }
            if mount["type"] == "bind":
                mount.update(read_only=len(parts) == 3 and parts[2] == "ro", bind={})
            else:
                mount["volume"] = {}
            mounts.append(mount)
        if mounts:
            service["volumes"] = mounts
        if "secrets" in service:
            service["secrets"] = [
                {"source": value, "target": f"/run/secrets/{value}"} for value in service["secrets"]
            ]
        if name == "worker":
            service["depends_on"] = {
                "retriever": {"condition": "service_started", "restart": True, "required": True}
            }
        if resolved:
            service.pop("env_file", None)
            if name in fixture.expected_environments:
                service["environment"] = {
                    **fixture.expected_environments[name],
                    **service.get("environment", {}),
                }
            if "mem_limit" in service:
                service["mem_limit"] = str(256 * 1024 * 1024)
    return model


def test_synthetic_models_keep_server_credential_free_and_worker_restricted(
    fixture: recovery.IngestionRecoveryFixture,
) -> None:
    recovery.validate_unresolved_model(fixture, _model(fixture, resolved=False))
    recovery.validate_resolved_model(fixture, _model(fixture, resolved=True))
    server = (fixture.directory / "server.py").read_text()
    for private in (
        recovery.BOOTSTRAP_PASSWORD,
        recovery.MIGRATION_PASSWORD,
        recovery.PROCESSOR_PASSWORD,
        "PRIMARY_SIGNAL_DATABASE",
        "psycopg",
        "sqlalchemy",
    ):
        assert private not in server
    login = (fixture.directory / "bootstrap-logins.sql").read_text()
    if fixture.mode == "article":
        capabilities = "primary_signal_cap_queue_consume,primary_signal_cap_article_persist"
        assert "primary_signal_cap_queue_submit" not in login
        assert "primary_signal_cap_feed_poll" not in login
    else:
        capabilities = (
            "primary_signal_cap_queue_consume,primary_signal_cap_queue_submit,"
            "primary_signal_cap_feed_poll"
        )
        assert "primary_signal_cap_article_persist" not in login
        assert "primary_signal_cap_feed_schedule" not in login
    assert f"GRANT {capabilities} TO {recovery.PROCESSOR_ROLE}" in login
    assert fixture.expected_compose["services"]["worker"]["command"][2] == (
        "ingestion" if fixture.mode == "feed" else "retrieval"
    )
    assert fixture.directory.stat().st_mode & 0o777 == 0o700
    assert (fixture.directory / "server.py").stat().st_mode & 0o777 == 0o444


@pytest.mark.parametrize(
    ("service", "key", "value"),
    [
        ("worker", "privileged", True),
        ("worker", "user", "0:0"),
        ("worker", "read_only", False),
        ("worker", "cap_drop", ["NET_RAW"]),
        ("worker", "security_opt", ["no-new-privileges:false"]),
        ("worker", "mem_limit", "536870912"),
        ("worker", "cpus", 2),
        ("worker", "network_mode", "host"),
        ("worker", "ports", [{"target": 8765, "published": "18765"}]),
        ("worker", "secrets", [{"source": "database_password"}]),
        ("worker", "command", ["primary-signal-processor", "--queue", "ingestion"]),
        ("worker", "depends_on", {"setup": {"condition": "service_completed_successfully"}}),
        ("retriever", "environment", {"PRIMARY_SIGNAL_DATABASE_URL": "synthetic-invalid"}),
        ("retriever", "volumes_from", ["operator-container"]),
        ("retriever", "command", ["python", "-c", "raise SystemExit(1)"]),
        ("setup", "entrypoint", []),
    ],
)
def test_resolved_model_rejects_runtime_boundary_changes(
    fixture: recovery.IngestionRecoveryFixture, service: str, key: str, value: object
) -> None:
    model = _model(fixture, resolved=True)
    model["services"][service][key] = value
    with pytest.raises(FixtureBoundaryError):
        recovery.validate_resolved_model(fixture, model)


@pytest.mark.parametrize(
    "change",
    [
        "external-network",
        "ingress",
        "foreign-volume",
        "root-mount",
        "operator-env",
        "privileged-worker-url",
    ],
)
def test_models_reject_foreign_files_networks_and_credentials(
    fixture: recovery.IngestionRecoveryFixture, change: str
) -> None:
    model = _model(fixture, resolved=True)
    if change == "external-network":
        model["networks"]["recovery_db"]["internal"] = False
    elif change == "ingress":
        model["networks"]["extra_ingress"] = {"name": "unexpected-ingress"}
    elif change == "foreign-volume":
        model["volumes"]["recovery-data"]["name"] = "operator-data"
    elif change == "root-mount":
        model["services"]["retriever"]["volumes"][0]["source"] = "/"
    elif change == "operator-env":
        model["services"]["worker"]["env_file"] = ["/synthetic-operator.env"]
    else:
        model["services"]["worker"]["environment"] = fixture.expected_environments["setup"]
    with pytest.raises(FixtureBoundaryError):
        recovery.validate_resolved_model(fixture, model)


def test_operator_environment_file_is_rejected_before_resolution(
    fixture: recovery.IngestionRecoveryFixture,
) -> None:
    model = _model(fixture, resolved=False)
    model["services"]["worker"]["env_file"] = ["/synthetic-operator.env"]
    with pytest.raises(FixtureBoundaryError, match="environment path changed"):
        recovery.validate_unresolved_model(fixture, model)


def test_bootstrap_ancestor_symlink_cannot_escape_checkout(
    fixture: recovery.IngestionRecoveryFixture, tmp_path: Path
) -> None:
    checkout = tmp_path / "synthetic-checkout"
    outside = tmp_path / "synthetic-outside" / "deploy"
    checkout.mkdir()
    init = outside / "postgres/initdb"
    init.mkdir(parents=True)
    for name in recovery.INIT_FILES:
        (init / name).write_text("-- synthetic external bootstrap fixture\n")
    (checkout / "deploy").symlink_to(outside, target_is_directory=True)
    escaped = replace(fixture, root=checkout)
    with pytest.raises(FixtureBoundaryError, match="bootstrap mount escapes checkout"):
        recovery.validate_unresolved_model(escaped, _model(fixture, resolved=False))


def test_manifest_include_is_rejected_before_compose_reads_it(
    fixture: recovery.IngestionRecoveryFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fixture.directory / "compose.synthetic.json"
    value = json.loads(path.read_text())
    value["include"] = ["/synthetic-operator.yaml"]
    path.write_text(json.dumps(value))
    execute = MagicMock(side_effect=AssertionError("unexpected Compose invocation"))
    monkeypatch.setattr(recovery.subprocess, "run", execute)
    with pytest.raises(FixtureBoundaryError, match="Compose file changed"):
        recovery.validate_fixture_models(fixture)
    execute.assert_not_called()


@pytest.mark.parametrize(
    "filename", ["server.py", "bootstrap-logins.sql", "worker.env", "database_password"]
)
def test_changed_fixture_content_or_symlink_cannot_reach_containers(
    fixture: recovery.IngestionRecoveryFixture, tmp_path: Path, filename: str
) -> None:
    path = fixture.directory / filename
    outside = tmp_path / "synthetic-private-file"
    outside.write_text("synthetic private marker")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(FixtureBoundaryError, match="file escaped fixture"):
        recovery.validate_unresolved_model(fixture, _model(fixture, resolved=False))


@pytest.fixture
def server(request: pytest.FixtureRequest) -> Iterator[Any]:
    namespace: dict[str, Any] = {"__name__": "synthetic_server_test"}
    exec(  # noqa: S102 - generated, fixed local fixture code only.
        compile(
            recovery.server_source(getattr(request, "param", "article")),
            "synthetic_server.py",
            "exec",
        ),
        namespace,
    )
    instance = namespace["FixtureServer"](0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.released.set()
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _request(
    server: Any, method: str, path: str, body: bytes | None = None
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_server_holds_only_first_fixed_article_request_and_returns_valid_extraction(
    server: Any,
) -> None:
    assert server.server_address[0] == "127.0.0.1"
    with RetrieverClient(f"http://127.0.0.1:{server.server_port}") as client:
        client.require_ready(ARTICLE_FETCH_CAPABILITY)
    body = json.dumps({"url": recovery.ARTICLE_URL, "etag": None, "last_modified": None}).encode()
    results: list[tuple[int, dict[str, Any]]] = []
    requester = threading.Thread(
        target=lambda: results.append(_request(server, "POST", "/v1/articles/fetch", body))
    )
    requester.start()
    try:
        assert server.first_started.wait(timeout=2)
        assert requester.is_alive()
        assert _request(server, "GET", "/control/state") == (
            200,
            {"first_started": True, "request_count": 1, "released": False},
        )
        assert _request(server, "POST", "/control/release", b"") == (200, {"released": True})
        requester.join(timeout=2)
        assert not requester.is_alive()
        assert results[0][0] == 200
        assert results[0][1]["extracted_text"] == recovery.EXPECTED_TEXT
        with RetrieverClient(f"http://127.0.0.1:{server.server_port}") as client:
            observed = client.fetch_article(recovery.ARTICLE_URL)
        assert observed.extraction is not None
        assert observed.extraction.normalized_content_hash == recovery.EXPECTED_NORMALIZED_HASH
        assert _request(server, "GET", "/control/state")[1]["request_count"] == 2
    finally:
        server.released.set()
        requester.join(timeout=2)


@pytest.mark.parametrize("server", ["article", "feed"], indirect=True)
@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"x" * 2049,
        b'{"url":"https://public.example/other","etag":null,"last_modified":null}',
        b'{"url":"a","url":"b","etag":null,"last_modified":null}',
    ],
)
def test_server_rejects_nonfixture_or_unbounded_payload_without_starting_hold(
    server: Any, body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    feed = _request(server, "GET", "/v1/ready")[1]["capabilities"] == [FEED_FETCH_CAPABILITY]
    endpoint = "/v1/feeds/fetch" if feed else "/v1/articles/fetch"
    assert _request(server, "POST", endpoint, body) == (
        400,
        {"error": "invalid_request"},
    )
    assert _request(server, "GET", "/control/state") == (
        200,
        {"first_started": False, "request_count": 0, "released": False},
    )
    assert _request(server, "GET", "/unknown")[0] == 404
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("server", ["feed"], indirect=True)
def test_feed_server_holds_first_request_then_returns_real_parseable_feed(server: Any) -> None:
    origin = f"http://127.0.0.1:{server.server_port}"
    with RetrieverClient(origin) as client:
        client.require_ready(FEED_FETCH_CAPABILITY)
    assert _request(server, "GET", "/v1/ready")[1]["capabilities"] == [FEED_FETCH_CAPABILITY]
    body = json.dumps({"url": recovery.FEED_URL, "etag": None, "last_modified": None}).encode()
    results: list[tuple[int, dict[str, Any]]] = []
    requester = threading.Thread(
        target=lambda: results.append(_request(server, "POST", "/v1/feeds/fetch", body))
    )
    requester.start()
    try:
        assert server.first_started.wait(timeout=2)
        assert requester.is_alive()
        assert _request(server, "GET", "/control/state")[1] == {
            "first_started": True,
            "request_count": 1,
            "released": False,
        }
        assert _request(server, "POST", "/control/release", b"") == (200, {"released": True})
        requester.join(timeout=2)
        assert not requester.is_alive()
        assert results[0][0] == 200
        with RetrieverClient(origin) as client:
            result = client.fetch(FeedPollTarget(recovery.FEED_ID, recovery.FEED_URL, None, None))
        assert result.status == 200
        assert result.final_url == recovery.FEED_URL
        assert result.body == recovery.FEED_BODY
        entries = parse_feed(result.body)
        assert len(entries) == 1
        assert entries[0].reported_url == recovery.ARTICLE_URL
        assert entries[0].reported_title == recovery.FEED_ENTRY_TITLE
        assert _request(server, "GET", "/control/state")[1]["request_count"] == 2
    finally:
        server.released.set()
        requester.join(timeout=2)


@pytest.mark.parametrize("server", ["article", "feed"], indirect=True)
def test_server_rejects_other_scenario_endpoint_and_url_before_hold(server: Any) -> None:
    feed = _request(server, "GET", "/v1/ready")[1]["capabilities"] == [FEED_FETCH_CAPABILITY]
    wrong_endpoint = "/v1/articles/fetch" if feed else "/v1/feeds/fetch"
    endpoint = "/v1/feeds/fetch" if feed else "/v1/articles/fetch"
    wrong_url = recovery.ARTICLE_URL if feed else recovery.FEED_URL
    body = json.dumps({"url": wrong_url, "etag": None, "last_modified": None}).encode()
    assert _request(server, "POST", wrong_endpoint, body)[0] == 404
    assert _request(server, "POST", endpoint, body)[0] == 400
    assert _request(server, "GET", "/control/state")[1] == {
        "first_started": False,
        "request_count": 0,
        "released": False,
    }


def test_unknown_mode_rejected_before_creating_fixture_files(tmp_path: Path) -> None:
    directory = tmp_path / "not-created"
    with pytest.raises(FixtureBoundaryError, match="invalid recovery mode"):
        recovery.create_fixture(
            ROOT,
            directory,
            "synthetic-ingestion",
            image="synthetic:local",
            mode="unknown",  # type: ignore[arg-type]
        )
    assert not directory.exists()
