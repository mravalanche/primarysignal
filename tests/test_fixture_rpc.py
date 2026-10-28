"""A single synthetic RPC peer cannot become a general network opt-in."""

from unittest.mock import MagicMock

import pytest

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.entrypoints import processor, retriever
from primary_signal.retrieval.fixture_rpc import (
    FIXTURE_RPC_HOST,
    FIXTURE_RPC_ORIGIN,
    FIXTURE_RPC_PORT,
    fixture_rpc_listener,
    fixture_rpc_origin,
)
from primary_signal.retrieval.policy import PolicyError, validate_target


def test_fixture_rpc_contract_accepts_one_literal_peer() -> None:
    assert FIXTURE_RPC_HOST == "192.0.2.10"
    assert FIXTURE_RPC_PORT == 8765
    assert FIXTURE_RPC_ORIGIN == "http://192.0.2.10:8765"
    assert fixture_rpc_listener("192.0.2.10", 8765) == ("192.0.2.10", 8765)
    assert fixture_rpc_origin("http://192.0.2.10:8765") == "http://192.0.2.10:8765"


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("0.0.0.0", 8765),  # noqa: S104 - rejected wildcard listener.
        ("127.0.0.1", 8765),
        ("192.0.2.11", 8765),
        ("192.168.1.1", 8765),
        ("8.8.8.8", 8765),
        ("retriever.example.test", 8765),
        ("::ffff:192.0.2.10", 8765),
        ("192.0.2.10 ", 8765),
        ("192.0.2.10", 80),
        ("192.0.2.10", "8765"),
        ("192.0.2.10", 8765.0),
        ("192.0.2.10", True),
        (None, 8765),
    ],
)
def test_fixture_listener_rejects_other_peers_and_implicit_coercion(
    host: object, port: object
) -> None:
    with pytest.raises(ValueError):
        fixture_rpc_listener(host, port)


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.0.2.10",
        "https://192.0.2.10:8765",
        "HTTP://192.0.2.10:8765",
        "http://192.0.2.10:8765/",
        "http://192.0.2.10:8765/v1/ready",
        "http://192.0.2.10:8765?",
        "http://192.0.2.10:8765#",
        "http://user@192.0.2.10:8765",
        "http://192.0.2.10:08765",
        "http://192.0.2.10.:8765",
        "http://192.0.2.010:8765",
        "http://192.0.2.11:8765",
        "http://127.0.0.1:8765",
        "http://192.168.1.1:8765",
        "http://8.8.8.8:8765",
        "http://retriever.example.test:8765",
        "http://[::ffff:192.0.2.10]:8765",
        " http://192.0.2.10:8765",
        "http://192.0.2.10:8765\n",
        None,
        True,
    ],
)
def test_fixture_origin_rejects_variants_without_canonicalizing(origin: object) -> None:
    with pytest.raises(ValueError):
        fixture_rpc_origin(origin)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_fixture_retriever_starts_only_fixed_listener(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    run = MagicMock()
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", environment)
    monkeypatch.setattr(retriever.uvicorn, "run", run)
    retriever.main(["--allow-fixture-rpc", "--host", "192.0.2.10", "--port", "8765"])
    run.assert_called_once()
    assert run.call_args.kwargs["host"] == "192.0.2.10"
    assert run.call_args.kwargs["port"] == 8765
    assert run.call_args.kwargs["proxy_headers"] is False
    assert run.call_args.kwargs["access_log"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        ["--allow-fixture-rpc"],
        ["--allow-fixture-rpc", "--host", "192.0.2.10"],
        ["--allow-fixture-rpc", "--port", "8765"],
        ["--allow-fixture-rpc", "--host", "0.0.0.0", "--port", "8765"],  # noqa: S104
        ["--allow-fixture-rpc", "--host", "192.0.2.10", "--port", "8766"],
        ["--allow-local-fetch", "--host", "192.0.2.10", "--port", "8765"],
        ["--allow-local-fetch", "--allow-fixture-rpc", "--host", "192.0.2.10", "--port", "8765"],
    ],
)
def test_retriever_rejects_missing_or_mixed_listener_profile_before_startup(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    run, app = MagicMock(), MagicMock()
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setattr(retriever.uvicorn, "run", run)
    monkeypatch.setattr(retriever, "create_retriever_app", app)
    with pytest.raises(SystemExit):
        retriever.main(arguments)
    run.assert_not_called()
    app.assert_not_called()


@pytest.mark.parametrize(
    ("flags", "origin"),
    [
        (["--allow-fixture-rpc"], "http://127.0.0.1:8765"),
        (["--allow-fixture-rpc"], "http://192.0.2.10:8765/"),
        (["--allow-fixture-rpc"], "http://192.0.2.11:8765"),
        (["--allow-local-fetch"], "http://192.0.2.10:8765"),
        (["--allow-local-fetch"], "http://127.0.0.1/"),
        (["--allow-local-fetch"], "http://127.0.0.1:8765/path"),
        (["--allow-local-fetch", "--allow-fixture-rpc"], "http://192.0.2.10:8765"),
        ([], "http://192.0.2.10:8765"),
    ],
)
def test_processor_rejects_missing_or_mixed_origin_profile_before_database(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], origin: str
) -> None:
    engine, client = MagicMock(), MagicMock()
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setattr(processor, "create_database_engine", engine)
    monkeypatch.setattr(processor, "RetrieverClient", client)
    with pytest.raises(SystemExit) as raised:
        processor.main(["--once", "--queue", "ingestion", "--retriever-origin", origin, *flags])
    assert raised.value.code == 2
    engine.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["processor", "retriever"])
@pytest.mark.parametrize("profile", ["local", "fixture"])
@pytest.mark.parametrize(
    ("raw_environment", "parsed_environment"),
    [(None, "development"), ("production", "test"), ("test", "production"), ("TEST", "test")],
)
def test_fixture_profile_checks_raw_and_parsed_environment_before_resources(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    profile: str,
    raw_environment: str | None,
    parsed_environment: str,
) -> None:
    parsed = Settings(environment=RuntimeEnvironment(parsed_environment))
    if raw_environment is None:
        monkeypatch.delenv("PRIMARY_SIGNAL_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", raw_environment)
    engine, run, app = MagicMock(), MagicMock(), MagicMock()
    module = processor if entrypoint == "processor" else retriever
    monkeypatch.setattr(module, "Settings", lambda: parsed)
    monkeypatch.setattr(processor, "create_database_engine", engine)
    monkeypatch.setattr(retriever.uvicorn, "run", run)
    monkeypatch.setattr(retriever, "create_retriever_app", app)
    host = "192.0.2.10" if profile == "fixture" else "127.0.0.1"
    args = ["--allow-fixture-rpc" if profile == "fixture" else "--allow-local-fetch"]
    args += (
        ["--once", "--queue", "ingestion", "--retriever-origin", f"http://{host}:8765"]
        if entrypoint == "processor"
        else ["--host", host, "--port", "8765"]
    )
    with pytest.raises(SystemExit):
        module.main(args)
    engine.assert_not_called()
    run.assert_not_called()
    app.assert_not_called()


def test_fixture_rpc_permission_does_not_allow_the_peer_as_a_fetch_target() -> None:
    resolver = MagicMock(return_value=["192.0.2.10"])
    for target in (
        "http://192.0.2.10/",
        "http://192.0.2.10:8765/",
        "https://fixture.example.test/",
    ):
        with pytest.raises(PolicyError):
            validate_target(target, resolver)
