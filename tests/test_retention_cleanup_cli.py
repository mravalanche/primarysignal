"""The maintenance command reports only identifiers and counts."""

import json
import uuid
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
