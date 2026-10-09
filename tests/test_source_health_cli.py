"""The local CLI must not disclose connection or role errors."""

from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.entrypoints import source_health


@pytest.mark.parametrize("error_type", [SQLAlchemyError, UnexpectedDatabaseRoleError])
def test_database_error_is_generic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    sentinel = "private-error-sentinel"
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", "postgresql+psycopg://x@localhost/db")
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "health_test")

    def fail(_settings: object) -> None:
        raise error_type(sentinel)

    monkeypatch.setattr(source_health, "create_database_engine", fail)
    with pytest.raises(SystemExit) as caught:
        source_health.main([])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "source health report unavailable\n"
    assert sentinel not in output.err


def test_invalid_settings_are_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", raising=False)
    with pytest.raises(SystemExit) as caught:
        source_health.main([])
    assert caught.value.code == 1
    assert capsys.readouterr().err == "source health report unavailable\n"


def test_missing_database_url_file_is_generic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "private-database-url"
    monkeypatch.delenv("PRIMARY_SIGNAL_DATABASE_URL", raising=False)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL_FILE", str(private_path))
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "health_test")
    with pytest.raises(SystemExit) as caught:
        source_health.main([])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "source health report unavailable\n"
    assert str(private_path) not in output.err
