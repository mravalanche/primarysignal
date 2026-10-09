from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import CheckConstraint, Engine, ForeignKeyConstraint
from sqlalchemy.pool import NullPool

from primary_signal.db import Base, DatabaseSettings
from primary_signal.db import engine as engine_module
from primary_signal.db.models import *  # noqa: F403 - verifies complete metadata registration
from primary_signal.web.admin.settings import (
    AdminDecisionDatabaseSettings,
    AdminEditorialDatabaseSettings,
)

EXPECTED_TABLES = {
    "admin_login_attempts",
    "admin_sessions",
    "article_urls",
    "articles",
    "content_versions",
    "content_version_retention",
    "feed_entries",
    "feed_poll_runs",
    "feeds",
    "fetch_attempts",
    "job_attempts",
    "jobs",
    "publication_events",
    "revision_signals",
    "revision_sources",
    "revision_tags",
    "signal_evidence",
    "sources",
    "stories",
    "story_revisions",
    "tags",
}


def test_database_settings_require_postgresql_psycopg() -> None:
    with pytest.raises(ValidationError, match=r"must use postgresql\+psycopg"):
        DatabaseSettings(url=SecretStr("sqlite:///local.db"), expected_role="app_test")

    with pytest.raises(ValidationError, match="database URL is invalid"):
        DatabaseSettings(url=SecretStr("not a url"), expected_role="app_test")


def test_database_settings_hide_url() -> None:
    settings = DatabaseSettings(
        url=SecretStr("postgresql+psycopg://app:example@db.public.example/app"),
        expected_role="app_test",
    )

    assert "example" not in repr(settings)
    assert settings.url.get_secret_value().startswith("postgresql+psycopg://")


@pytest.mark.parametrize(
    ("prefix", "settings_type"),
    [
        ("PRIMARY_SIGNAL_DATABASE_", DatabaseSettings),
        ("PRIMARY_SIGNAL_ADMIN_EDITORIAL_DATABASE_", AdminEditorialDatabaseSettings),
        ("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_", AdminDecisionDatabaseSettings),
    ],
)
def test_database_url_file_for_each_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prefix: str,
    settings_type: type[DatabaseSettings],
) -> None:
    url_value = "postgresql+psycopg://reader:example@db.public.example/app"  # pragma: allowlist secret
    file_path = tmp_path / "database-url"
    file_path.write_text(url_value + "\n", encoding="utf-8")
    monkeypatch.delenv(prefix + "URL", raising=False)
    monkeypatch.setenv(prefix + "URL_FILE", str(file_path))
    monkeypatch.setenv(prefix + "EXPECTED_ROLE", "app_test")

    settings = settings_type()

    assert settings.url.get_secret_value() == url_value
    assert url_value not in repr(settings)


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"\n",
        b"not-a-url\n",
        b"postgresql+psycopg://x@db.public.example/app\nother",
        b"postgresql+psycopg://x@db.public.example/app\x00extra",
        b"postgresql+psycopg://x@db.public.example/app\tmore",
        b"x" * 4097,
        b"\xff",
    ],
)
def test_database_url_file_rejects_invalid_contents_without_leak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: bytes
) -> None:
    prefix = "PRIMARY_SIGNAL_DATABASE_"
    file_path = tmp_path / "private-path"
    file_path.write_bytes(contents)
    monkeypatch.delenv(prefix + "URL", raising=False)
    monkeypatch.setenv(prefix + "URL_FILE", str(file_path))
    monkeypatch.setenv(prefix + "EXPECTED_ROLE", "app_test")

    with pytest.raises((ValueError, ValidationError)) as caught:
        DatabaseSettings()
    assert str(file_path) not in str(caught.value)
    assert "other" not in str(caught.value)


def test_database_url_file_rejects_missing_and_conflicting_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = "PRIMARY_SIGNAL_DATABASE_"
    file_path = tmp_path / "private-path"
    monkeypatch.setenv(prefix + "URL_FILE", str(file_path))
    monkeypatch.delenv(prefix + "URL", raising=False)
    with pytest.raises(ValueError, match="could not be read") as caught:
        DatabaseSettings(expected_role="app_test")
    assert str(file_path) not in str(caught.value)

    file_path.write_text("postgresql+psycopg://x@db.public.example/app", encoding="utf-8")
    monkeypatch.setenv(prefix + "URL", "postgresql+psycopg://other@db.public.example/app")
    with pytest.raises(ValueError, match="cannot both be set"):
        DatabaseSettings(expected_role="app_test")


def test_metadata_registers_only_expected_schema_tables() -> None:
    tables = {table.name for table in Base.metadata.sorted_tables}

    assert tables == EXPECTED_TABLES
    assert {table.schema for table in Base.metadata.sorted_tables} == {"primary_signal"}


def test_metadata_contains_versioned_identity_and_provenance_contract() -> None:
    required_columns = {
        "feeds": {"normalized_url", "url_hash", "url_normalization_version", "last_success_at"},
        "feed_entries": {"identity_method", "identity_version", "identity_key", "article_id"},
        "articles": {"current_canonical_url_id", "current_content_version_id", "last_seen_at"},
        "article_urls": {"original_url", "normalized_url", "normalization_version", "kind"},
        "fetch_attempts": {"retrieval_strategy", "redirect_chain", "resulting_content_version_id"},
        "content_versions": {
            "raw_response_hash",
            "normalized_content_hash",
            "normalization_version",
            "extractor_version",
            "fetched_at",
        },
        "content_version_retention": {"content_version_id", "superseded_at"},
        "jobs": {
            "job_type",
            "payload_version",
            "deduplication_key",
            "lease_token",
            "lease_expires_at",
        },
        "job_attempts": {"attempt_number", "initial_lease_expires_at"},
    }

    for table_name, columns in required_columns.items():
        table = Base.metadata.tables[f"primary_signal.{table_name}"]
        assert columns <= set(table.columns.keys())


def test_all_foreign_keys_restrict_deletion() -> None:
    foreign_keys = [
        constraint
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert foreign_keys
    assert all(element.ondelete == "RESTRICT" for fk in foreign_keys for element in fk.elements)


def test_status_columns_are_guarded_by_checks() -> None:
    status_tables = {
        table.name
        for table in Base.metadata.sorted_tables
        if "status" in table.columns
        and any(isinstance(item, CheckConstraint) for item in table.constraints)
    }

    assert status_tables == {
        "feed_poll_runs",
        "fetch_attempts",
        "job_attempts",
        "jobs",
        "story_revisions",
    }


def test_migration_configuration_contains_no_database_url() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert "sqlalchemy.url" not in (repository_root / "alembic.ini").read_text()
    migration = (
        repository_root / "migrations/versions/20261001_01_initial_ingestion_schema.py"
    ).read_text()
    assert "CREATE TRIGGER protect_content_versions" in migration
    assert "raw_html" not in migration
    assert "raw_body" not in migration


class ScalarResult:
    def __init__(self, value: str) -> None:
        self.value = value

    def scalar_one(self) -> str:
        return self.value


def test_database_role_assertion_accepts_only_expected_role() -> None:
    connection = MagicMock()
    connection.execute.return_value = ScalarResult("processor_test")

    engine_module.assert_database_role(connection, "processor_test")

    with pytest.raises(engine_module.UnexpectedDatabaseRoleError, match="does not match"):
        engine_module.assert_database_role(connection, "scheduler_test")


def test_engine_construction_and_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock(spec=Engine)
    captured: dict[str, object] = {}

    def fake_create_engine(url: str, **options: object) -> Engine:
        captured.update(url=url, options=options)
        return fake_engine

    def fake_listens_for(target: object, event_name: str) -> Callable[[object], object]:
        captured.update(target=target, event_name=event_name)

        def register(listener: object) -> object:
            captured["listener"] = listener
            return listener

        return register

    monkeypatch.setattr(engine_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(engine_module.event, "listens_for", fake_listens_for)
    settings = DatabaseSettings(
        url=SecretStr("postgresql+psycopg://app:local@db.public.example/app"),
        expected_role="app_test",
    )

    assert engine_module.create_database_engine(settings) is fake_engine
    assert captured["url"] == settings.url.get_secret_value()
    assert captured["event_name"] == "engine_connect"
    options = captured["options"]
    assert isinstance(options, dict)
    options = cast(dict[str, object], options)
    assert options["hide_parameters"] is True
    assert options["echo"] is False
    assert options["max_overflow"] == 5
    connect_args = options["connect_args"]
    assert isinstance(connect_args, dict)
    connect_args = cast(dict[str, object], connect_args)
    assert connect_args["connect_timeout"] == 10
    assert connect_args["application_name"] == "primary_signal"
    connection_options = connect_args["options"]
    assert isinstance(connection_options, str)
    assert "timezone=UTC" in connection_options
    assert "statement_timeout=30000" in connection_options
    assert "search_path=pg_catalog,primary_signal" in connection_options
    assert "lock_timeout=" in connection_options
    assert "idle_in_transaction_session_timeout=" in connection_options

    assert engine_module.create_database_engine(settings, pool_class=NullPool) is fake_engine
    second_options = captured["options"]
    assert isinstance(second_options, dict)
    assert second_options["poolclass"] is NullPool
    assert "pool_size" not in second_options

    factory = engine_module.session_factory(fake_engine)
    assert factory.kw["bind"] is fake_engine


def test_compose_uses_an_ignored_password_file_and_no_default_host_port() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    compose = (repository_root / "compose.yaml").read_text()
    ignore = (repository_root / ".gitignore").read_text()
    migration_environment = (repository_root / "migrations/env.py").read_text()

    assert "POSTGRES_PASSWORD_FILE" in compose
    assert ".secrets/database_password" in compose
    assert "ports:" not in compose
    assert ".secrets/*" in ignore
    assert "pool_class=NullPool" in migration_environment
    assert "pg_advisory_xact_lock" in migration_environment
