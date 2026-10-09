"""The maintenance command reports only identifiers and counts."""

import json
import uuid
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings
from primary_signal.entrypoints import retention_cleanup


def test_cli_reports_bounded_identifiers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = Mock(spec=Engine)
    identifier = uuid.uuid7()
    monkeypatch.setattr(
        retention_cleanup, "DatabaseSettings", lambda: cast(DatabaseSettings, object())
    )

    def fake_engine(_settings: DatabaseSettings) -> Engine:
        return cast(Engine, engine)

    monkeypatch.setattr(retention_cleanup, "create_database_engine", fake_engine)
    observed: list[int] = []

    def fake_run(_engine: Engine, *, limit: int) -> tuple[str, ...]:
        observed.append(limit)
        return (str(identifier),)

    monkeypatch.setattr(retention_cleanup, "run_batch", fake_run)
    retention_cleanup.main(["--limit", "1"])
    assert json.loads(capsys.readouterr().out) == {
        "count": 1,
        "content_version_ids": [str(identifier)],
    }
    assert observed == [1]
    engine.dispose.assert_called_once()


def test_cli_rejects_unbounded_batch_without_opening_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = Mock()
    monkeypatch.setattr(retention_cleanup, "create_database_engine", factory)
    with pytest.raises(SystemExit) as raised:
        retention_cleanup.main(["--limit", "501"])
    assert raised.value.code == 2
    factory.assert_not_called()


def test_cli_hides_database_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "private-error-sentinel"
    monkeypatch.setattr(
        retention_cleanup, "DatabaseSettings", lambda: cast(DatabaseSettings, object())
    )

    def fail(_settings: DatabaseSettings) -> Engine:
        raise SQLAlchemyError(sentinel)

    monkeypatch.setattr(retention_cleanup, "create_database_engine", fail)
    with pytest.raises(SystemExit) as raised:
        retention_cleanup.main([])
    assert raised.value.code == "retention cleanup unavailable"
    assert sentinel not in capsys.readouterr().err


def test_cli_uses_file_backed_maintenance_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    file_path = tmp_path / "maintenance-url"
    file_path.write_text("postgresql+psycopg://maintainer@db.public.example/app\n")
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL", raising=False)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", str(file_path))
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "maintainer")
    engine = Mock(spec=Engine)

    def fake_engine(settings: DatabaseSettings) -> Engine:
        assert (
            settings.url.get_secret_value()
            == "postgresql+psycopg://maintainer@db.public.example/app"
        )
        assert settings.expected_role == "maintainer"
        return cast(Engine, engine)

    def fake_run_batch(_engine: Engine, *, limit: int) -> tuple[str, ...]:
        assert limit == 100
        return ()

    monkeypatch.setattr(retention_cleanup, "create_database_engine", fake_engine)
    monkeypatch.setattr(retention_cleanup, "run_batch", fake_run_batch)
    retention_cleanup.main([])
    assert json.loads(capsys.readouterr().out) == {"count": 0, "content_version_ids": []}
    engine.dispose.assert_called_once()


def test_cli_hides_database_url_file_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    file_path = tmp_path / "private-maintenance-url"
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL", raising=False)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", str(file_path))
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "maintainer")
    with pytest.raises(SystemExit) as raised:
        retention_cleanup.main([])
    assert raised.value.code == "retention cleanup unavailable"
    assert str(file_path) not in capsys.readouterr().err
