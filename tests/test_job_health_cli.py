"""The operator CLI bounds output and conceals database failure details."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings
from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.entrypoints import job_health
from primary_signal.jobs.health import HealthReport


def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", "postgresql+psycopg://x@localhost/db")
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "job_health_test")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], (None, None, 100)),
        (
            ["--queue", "unknown.queue", "--status", "dead", "--limit", "1"],
            ("unknown.queue", "dead", 1),
        ),
    ],
)
def test_cli_passes_filters_and_prints_compact_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected: tuple[str | None, str | None, int],
) -> None:
    _settings(monkeypatch)
    engine, connection = MagicMock(spec=Engine), MagicMock(spec=Connection)
    engine.connect.return_value.__enter__.return_value = connection
    report = HealthReport(datetime(2026, 10, 28, tzinfo=UTC), [], [], False, False)

    def create_engine(settings: DatabaseSettings) -> Engine:
        assert settings.expected_role == "job_health_test"
        return engine

    def read_health(
        actual: Connection, *, queue: str | None, status: str | None, limit: int
    ) -> HealthReport:
        assert actual is connection
        assert (queue, status, limit) == expected
        return report

    monkeypatch.setattr(job_health, "create_database_engine", create_engine)
    monkeypatch.setattr(job_health, "read_health", read_health)
    job_health.main(arguments)
    output = capsys.readouterr()
    assert output.out == json.dumps(report.as_dict(), separators=(",", ":")) + "\n"
    assert output.err == ""
    engine.dispose.assert_called_once()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--queue", "bad/private-sentinel"],
        ["--status", "private-sentinel"],
        ["--limit", "0"],
        ["--limit", "501"],
    ],
)
def test_invalid_filters_fail_before_database_setup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    create_engine = MagicMock()
    monkeypatch.setattr(job_health, "create_database_engine", create_engine)
    with pytest.raises(SystemExit) as raised:
        job_health.main(arguments)
    assert raised.value.code == 2
    create_engine.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-sentinel" not in output.err


@pytest.mark.parametrize("error_type", [SQLAlchemyError, UnexpectedDatabaseRoleError, ValueError])
def test_database_setup_failure_is_generic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    _settings(monkeypatch)

    def fail(_settings: DatabaseSettings) -> Engine:
        raise error_type("private-error-sentinel")

    monkeypatch.setattr(job_health, "create_database_engine", fail)
    with pytest.raises(SystemExit) as raised:
        job_health.main([])
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "job health report unavailable\n"


@pytest.mark.parametrize("stage", ["connect", "read", "dispose"])
def test_database_failure_disposes_engine_without_disclosing_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stage: str
) -> None:
    _settings(monkeypatch)
    engine = MagicMock(spec=Engine)

    def create_engine(_settings: DatabaseSettings) -> Engine:
        return engine

    read_health = MagicMock(
        return_value=HealthReport(datetime(2026, 10, 28, tzinfo=UTC), [], [], False, False)
    )
    error = SQLAlchemyError("private-error-sentinel")
    if stage == "connect":
        engine.connect.side_effect = error
    elif stage == "read":
        read_health.side_effect = error
    else:
        engine.dispose.side_effect = error
    monkeypatch.setattr(job_health, "create_database_engine", create_engine)
    monkeypatch.setattr(job_health, "read_health", read_health)
    with pytest.raises(SystemExit) as raised:
        job_health.main([])
    assert raised.value.code == 1
    engine.dispose.assert_called_once()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "job health report unavailable\n"


def test_invalid_settings_are_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", raising=False)
    with pytest.raises(SystemExit) as raised:
        job_health.main([])
    assert raised.value.code == 1
    assert capsys.readouterr().err == "job health report unavailable\n"


def test_missing_database_url_file_is_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _settings(monkeypatch)
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL")
    private_path = tmp_path / "private-database-url"
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", str(private_path))
    with pytest.raises(SystemExit) as raised:
        job_health.main([])
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "job health report unavailable\n"
    assert str(private_path) not in output.err
