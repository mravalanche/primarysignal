"""Source controls print safe results only after the transaction commits."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings
from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.entrypoints import source_control
from primary_signal.sources.control import SourceControlResult

ARGUMENTS = ["pause", "example-source", "--reason", "private-audit-marker"]


def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", "postgresql+psycopg://x@localhost/db")
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "source_control_test")


@pytest.mark.parametrize(
    ("action", "enabled", "changed"),
    [
        ("pause", False, True),
        ("resume", True, True),
        ("resume", True, False),
    ],
)
def test_cli_commits_before_printing_and_never_echoes_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    enabled: bool,
    changed: bool,
) -> None:
    _settings(monkeypatch)
    engine, connection = MagicMock(spec=Engine), MagicMock(spec=Connection)
    events: list[str] = []
    result = SourceControlResult(
        "example-source",
        enabled,
        changed,
        datetime(2026, 10, 28, tzinfo=UTC),
        uuid.uuid4() if changed else None,
    )

    def create_engine(settings: DatabaseSettings) -> Engine:
        assert settings.expected_role == "source_control_test"
        return engine

    def begin() -> Connection:
        events.append("begin")
        return connection

    def finish(*exception: object) -> None:
        assert exception == (None, None, None)
        assert capsys.readouterr().out == ""
        events.append("commit")

    def transition(
        actual: Connection, source_key: str, *, enabled: bool, reason: str
    ) -> SourceControlResult:
        assert actual is connection
        assert source_key == "example-source"
        assert enabled is result.enabled
        assert reason == "private-audit-marker"
        events.append("transition")
        return result

    engine.begin.return_value.__enter__.side_effect = begin
    engine.begin.return_value.__exit__.side_effect = finish
    engine.dispose.side_effect = lambda: events.append("dispose")
    monkeypatch.setattr(source_control, "create_database_engine", create_engine)
    monkeypatch.setattr(source_control, "set_source_enabled", transition)
    source_control.main([action, "example-source", "--reason", "\u00a0private-audit-marker\u3000"])
    assert events == ["begin", "transition", "commit", "dispose"]
    output = capsys.readouterr()
    assert output.out == json.dumps(result.as_dict(), separators=(",", ":")) + "\n"
    assert output.err == ""
    assert "private-audit-marker" not in output.out
    engine.dispose.assert_called_once()


@pytest.mark.parametrize(
    "arguments",
    [
        ["pause", "example-source"],
        ["pause", "bad/private-marker", "--reason", "review"],
        ["pause", "example-source", "--reason", " "],
        ["pause", "example-source", "--reason", "\u00a0"],
        ["pause", "example-source", "--reason", "private-marker\n"],
        ["pause", "example-source", "--reason", "private-marker" * 100],
    ],
)
def test_invalid_arguments_fail_before_database_without_echoing_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    create_engine = MagicMock()
    monkeypatch.setattr(source_control, "create_database_engine", create_engine)
    with pytest.raises(SystemExit) as raised:
        source_control.main(arguments)
    assert raised.value.code == 2
    create_engine.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-marker" not in output.err


@pytest.mark.parametrize("error_type", [SQLAlchemyError, UnexpectedDatabaseRoleError, ValueError])
def test_database_setup_failure_is_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error_type: type[Exception]
) -> None:
    _settings(monkeypatch)

    def fail(_settings: DatabaseSettings) -> Engine:
        raise error_type("private-error-marker")

    monkeypatch.setattr(source_control, "create_database_engine", fail)
    with pytest.raises(SystemExit) as raised:
        source_control.main(ARGUMENTS)
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "source control unavailable\n"


@pytest.mark.parametrize("stage", ["begin", "transition", "commit", "dispose"])
def test_transaction_failure_never_prints_success_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stage: str
) -> None:
    _settings(monkeypatch)
    engine = MagicMock(spec=Engine)

    def create_engine(_settings: DatabaseSettings) -> Engine:
        return engine

    transition = MagicMock(
        return_value=SourceControlResult(
            "example-source", False, True, datetime(2026, 10, 28, tzinfo=UTC), uuid.uuid4()
        )
    )
    error = SQLAlchemyError("private-error-marker")
    if stage == "begin":
        engine.begin.side_effect = error
    elif stage == "transition":
        transition.side_effect = error
    elif stage == "commit":
        engine.begin.return_value.__exit__.side_effect = error
    else:
        engine.dispose.side_effect = error
    monkeypatch.setattr(source_control, "create_database_engine", create_engine)
    monkeypatch.setattr(source_control, "set_source_enabled", transition)
    with pytest.raises(SystemExit) as raised:
        source_control.main(ARGUMENTS)
    assert raised.value.code == 1
    engine.dispose.assert_called_once()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "source control unavailable\n"


def test_missing_database_url_file_is_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _settings(monkeypatch)
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL")
    private_path = tmp_path / "private-database-url"
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", str(private_path))
    with pytest.raises(SystemExit) as raised:
        source_control.main(ARGUMENTS)
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "source control unavailable\n"
    assert str(private_path) not in output.err
