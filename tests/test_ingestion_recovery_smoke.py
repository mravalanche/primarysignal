"""Safety and correctness gates for the disposable container recovery proof."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from scripts import smoke_ingestion_recovery as smoke
from scripts.ingestion_recovery_fixture import IngestionRecoveryFixture

IDENTIFIER = "1" * 64


def fixture(tmp_path: Path) -> IngestionRecoveryFixture:
    return cast(
        IngestionRecoveryFixture,
        SimpleNamespace(
            root=tmp_path,
            directory=tmp_path,
            project="synthetic-project",
            image="synthetic-project:local",
            env={"PATH": os.defpath},
            docker_args=("docker",),
            compose_args=("compose",),
            database_name="synthetic_recovery_test",
            bootstrap_role="synthetic_bootstrap_test",
            migration_role="synthetic_migration_test",
            processor_role="synthetic_processor_test",
        ),
    )


def state(**changes: Any) -> dict[str, Any]:
    return {
        "id": IDENTIFIER,
        "image": "sha256:" + "2" * 64,
        "labels": {
            "com.docker.compose.project": "synthetic-project",
            "com.docker.compose.service": "worker",
            "com.docker.compose.oneoff": "False",
        },
        "status": "running",
        "exit_code": 0,
        "started_at": "first",
        "health": None,
        "restart": "no",
        "network_mode": "container:" + "3" * 64,
        "ports": {"8000/tcp": None},
    } | changes


@pytest.mark.parametrize(
    "change",
    [
        {
            "labels": {
                "com.docker.compose.project": "other-project",
                "com.docker.compose.service": "worker",
                "com.docker.compose.oneoff": "False",
            }
        },
        {
            "labels": {
                "com.docker.compose.project": "synthetic-project",
                "com.docker.compose.service": "retriever",
                "com.docker.compose.oneoff": "False",
            }
        },
        {
            "labels": {
                "com.docker.compose.project": "synthetic-project",
                "com.docker.compose.service": "worker",
                "com.docker.compose.oneoff": "True",
            }
        },
        {"restart": "always"},
        {"ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15432"}]}},
        {"id": "4" * 64},
        {"labels": None},
        {"ports": []},
    ],
)
def test_foreign_or_unsafe_container_is_never_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, Any]
) -> None:
    calls: list[tuple[str, ...]] = []

    def docker(_fixture: IngestionRecoveryFixture, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        assert args[0] == "inspect"
        return json.dumps(state(**change))

    monkeypatch.setattr(smoke, "_docker", docker)
    with pytest.raises(smoke.SmokeFailure, match="owned container boundary"):
        smoke._kill_worker(fixture(tmp_path), IDENTIFIER)  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 1


def test_kill_signals_only_verified_worker_and_requires_exit_137(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def docker(_fixture: IngestionRecoveryFixture, *args: str, **_kwargs: Any) -> str:
        calls.append(args)
        return json.dumps(state()) if args[0] == "inspect" else ""

    def wait(
        _fixture: IngestionRecoveryFixture, service: str, identifier: str, **kwargs: Any
    ) -> dict[str, Any]:
        assert (service, identifier) == ("worker", IDENTIFIER)
        assert kwargs == {"status": "exited", "exit_code": 137}
        return state(status="exited", exit_code=137)

    monkeypatch.setattr(smoke, "_docker", docker)
    monkeypatch.setattr(smoke, "_wait", wait)
    smoke._kill_worker(fixture(tmp_path), IDENTIFIER)  # pyright: ignore[reportPrivateUsage]
    assert calls[1] == ("kill", "--signal", "KILL", IDENTIFIER)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"fetches": ["running"]},
        {"version_count": 1},
        {"current": "synthetic-version"},
        {"search_count": 1},
    ],
)
def test_no_partial_results_gate_rejects_each_processing_effect(change: dict[str, Any]) -> None:
    snapshot: dict[str, Any] = {
        "fetches": [],
        "version_count": 0,
        "current": None,
        "search_count": 0,
    }
    snapshot.update(change)
    with pytest.raises(smoke.SmokeFailure, match="partial article observations"):
        smoke._no_observations(snapshot)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "change",
    [
        {"status": "running"},
        {"attempt_count": 1},
        {"attempts": ["succeeded"]},
        {"fetches": ["fetched", "fetched"]},
        {"version_count": 2},
        {"single_current": False},
        {"hash_matches": False},
        {"search_count": 0},
        {"search_matches": False},
        {"current": None},
    ],
)
def test_completion_requires_history_single_version_and_search(change: dict[str, Any]) -> None:
    snapshot = {
        "status": "succeeded",
        "attempt_count": 2,
        "attempts": ["lease_expired", "succeeded"],
        "fetches": ["fetched"],
        "version_count": 1,
        "single_current": True,
        "hash_matches": True,
        "search_count": 1,
        "search_matches": True,
        "current": "synthetic-version",
    } | change
    with pytest.raises(smoke.SmokeFailure, match="recovered article result"):
        smoke._complete(snapshot)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "value",
    [
        {"first_started": True, "released": False, "request_count": True},
        {"first_started": 1, "released": False, "request_count": 1},
        {"first_started": True, "released": False, "request_count": -1},
        {"first_started": True, "released": False, "request_count": 1, "unexpected": "field"},
    ],
)
def test_control_rejects_coerced_or_unexpected_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: dict[str, Any]
) -> None:
    def compose(_fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
        return json.dumps(value)

    monkeypatch.setattr(smoke, "_compose", compose)
    with pytest.raises(smoke.SmokeFailure, match="synthetic fetch control"):
        smoke._control(fixture(tmp_path))  # pyright: ignore[reportPrivateUsage]


def test_fencing_uses_restricted_worker_and_private_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = {"lease_token": "private-synthetic-token", "worker_id": "private-synthetic-worker"}

    def compose(_fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
        assert args[:5] == ("run", "--rm", "--no-deps", "-T", "worker")
        assert kwargs["timeout"] == 30
        assert json.loads(kwargs["input_text"])["lease"] == lease
        assert all("private-synthetic" not in argument for argument in args)
        assert "TransactionalJobQueue" in args[-1] and "except LostLease:" in args[-1]
        return '{"fenced":true}'

    monkeypatch.setattr(smoke, "_compose", compose)
    assert smoke._probe(fixture(tmp_path), "fence", lease=lease) == {"fenced": True}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("source", [smoke.DATABASE_PROBE, smoke.FENCE_PROBE, smoke.CONTROL])
def test_embedded_probe_python_is_valid(source: str) -> None:
    compile(source, "synthetic-private-probe", "exec")


def test_container_wait_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = [0.0]
    monkeypatch.setattr(smoke, "monotonic", lambda: now[0])

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def inspect(*args: Any) -> dict[str, Any]:
        return state(status="running")

    monkeypatch.setattr(smoke, "sleep", sleep)
    monkeypatch.setattr(smoke, "_inspect", inspect)
    with pytest.raises(smoke.SmokeFailure, match="bounded container wait"):
        smoke._wait(  # pyright: ignore[reportPrivateUsage]
            fixture(tmp_path), "worker", IDENTIFIER, status="exited", exit_code=0, timeout=1
        )
    assert now[0] == 1


def test_namespace_owner_must_stay_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    owner = state()

    def inspect(*args: Any) -> dict[str, Any]:
        return state(status="exited")

    monkeypatch.setattr(smoke, "_inspect", inspect)
    assert not smoke._owner_unchanged(fixture(tmp_path), owner)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("down_failed", [True, False])
def test_cleanup_stays_scoped_and_removes_image_even_after_down_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, down_failed: bool
) -> None:
    owned = fixture(tmp_path)
    image_exists = [True]
    commands: list[tuple[str, ...]] = []

    def compose(_fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
        assert args == ("down", "--volumes", "--remove-orphans", "--timeout", "10")
        if down_failed:
            raise smoke.SmokeFailure("cleanup")
        return ""

    def exists(_fixture: IngestionRecoveryFixture, kind: str, name: str) -> bool:
        if kind == "image":
            assert name == owned.image
            return image_exists[0]
        assert name.startswith(owned.project + "_")
        return False

    def docker(_fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
        commands.append(args)
        if args[:2] == ("image", "rm"):
            assert args == ("image", "rm", owned.image)
            image_exists[0] = False
        else:
            assert args == (
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + owned.project,
            )
        return ""

    monkeypatch.setattr(smoke, "_compose", compose)
    monkeypatch.setattr(smoke, "_exists", exists)
    monkeypatch.setattr(smoke, "_docker", docker)
    if down_failed:
        with pytest.raises(smoke.SmokeFailure, match=r"^cleanup$"):
            smoke._cleanup(owned)  # pyright: ignore[reportPrivateUsage]
    else:
        smoke._cleanup(owned)  # pyright: ignore[reportPrivateUsage]
    assert not image_exists[0]
    assert len(commands) == 2


def test_release_accepts_only_acknowledgement_then_rechecks_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    operations: list[str] = []

    def compose(_fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
        operations.append(args[-1])
        return json.dumps(
            {"released": True}
            if args[-1] == "release"
            else {"released": True, "first_started": True, "request_count": 1}
        )

    monkeypatch.setattr(smoke, "_compose", compose)
    assert smoke._control(fixture(tmp_path), "release") == {  # pyright: ignore[reportPrivateUsage]
        "released": True,
        "first_started": True,
        "request_count": 1,
    }
    assert operations == ["release", "state"]


@pytest.mark.parametrize(
    "acknowledgement",
    [
        {"released": 1},
        {"released": False},
        {},
        {"released": True, "extra": True},
    ],
)
def test_release_rejects_invalid_acknowledgement_without_more_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, acknowledgement: dict[str, Any]
) -> None:
    operations: list[str] = []

    def compose(_fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
        operations.append(args[-1])
        return json.dumps(acknowledgement)

    monkeypatch.setattr(smoke, "_compose", compose)
    with pytest.raises(smoke.SmokeFailure, match="synthetic fetch control"):
        smoke._control(fixture(tmp_path), "release")  # pyright: ignore[reportPrivateUsage]
    assert operations == ["release"]
