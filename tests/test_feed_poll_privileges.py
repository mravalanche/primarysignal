"""PostgreSQL privilege checks for feed poll ingestion."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

ROLE = "primary_signal_cap_feed_poll"


@pytest.fixture
def poll_privilege_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, Engine]]:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    processor_url = os.environ.get("PRIMARY_SIGNAL_TEST_PROCESSOR_DATABASE_URL")
    if not admin_url or not expected_role or not processor_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires the admin and restricted processor DSNs")
        pytest.skip("set test admin and processor DSNs for PostgreSQL privilege tests")

    admin = create_engine(admin_url, hide_parameters=True)
    processor = create_engine(processor_url, hide_parameters=True)
    with admin.connect() as connection:
        database_name, current_user = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        assert str(database_name).endswith("_test")
        assert current_user == expected_role
        assert connection.execute(
            text("SELECT to_regrole(:role) IS NOT NULL"), {"role": ROLE}
        ).scalar_one(), "run scripts/bootstrap_test_database_roles.py before migrations"
    with processor.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == "processor_test"

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        yield admin, processor
    finally:
        admin.dispose()
        processor.dispose()


@pytest.mark.postgres
def test_feed_poll_capability_is_non_login_and_column_scoped(
    poll_privilege_engines: tuple[Engine, Engine],
) -> None:
    admin, processor = poll_privilege_engines
    with admin.connect() as connection:
        role = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=:role"
            ),
            {"role": ROLE},
        ).one()
        assert not any(role)
        assert connection.execute(
            text("SELECT pg_has_role('processor_test', :role, 'member')"),
            {"role": ROLE},
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT has_function_privilege(:role, "
                "'primary_signal.lock_source_enabled(uuid)', 'EXECUTE')"
            ),
            {"role": ROLE},
        ).scalar_one()

        allowed = (
            ("feeds", "configured_url", "SELECT"),
            ("feeds", "normalized_url", "SELECT"),
            ("feeds", "enabled", "SELECT"),
            ("sources", "enabled", "SELECT"),
            ("feeds", "consecutive_failures", "UPDATE"),
            ("feed_poll_runs", "entries_discovered", "UPDATE"),
            ("feed_poll_runs", "status", "UPDATE"),
            ("feed_entries", "identity_key", "INSERT"),
            ("feed_entries", "article_id", "SELECT"),
            ("feed_entries", "last_seen_at", "UPDATE"),
            ("articles", "last_seen_at", "SELECT"),
            ("articles", "current_canonical_url_id", "UPDATE"),
            ("article_urls", "normalized_url_hash", "INSERT"),
            ("article_urls", "last_seen_at", "SELECT"),
        )
        denied = (
            ("feeds", "enabled", "UPDATE"),
            ("feeds", "next_poll_at", "UPDATE"),
            ("feed_entries", "reported_title", "UPDATE"),
            ("article_urls", "normalized_url", "UPDATE"),
            ("sources", "name", "SELECT"),
            ("sources", "enabled", "UPDATE"),
            ("jobs", "id", "UPDATE"),
        )
        for table, column, privilege in allowed:
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, "
                    "'primary_signal.' || :table, :column, :privilege)"
                ),
                {"role": ROLE, "table": table, "column": column, "privilege": privilege},
            ).scalar_one()
        for table, column, privilege in denied:
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege(:role, "
                    "'primary_signal.' || :table, :column, :privilege)"
                ),
                {"role": ROLE, "table": table, "column": column, "privilege": privilege},
            ).scalar_one()
        assert not connection.execute(
            text("SELECT has_table_privilege(:role, 'primary_signal.articles', 'DELETE')"),
            {"role": ROLE},
        ).scalar_one()

    for statement in (
        "SELECT enabled FROM primary_signal.sources FOR SHARE",
        "UPDATE primary_signal.feeds SET enabled=false",
        "UPDATE primary_signal.feed_entries SET reported_title='forbidden'",
        "DELETE FROM primary_signal.article_urls",
    ):
        with pytest.raises(DBAPIError), processor.begin() as connection:
            connection.execute(text(statement))


@pytest.mark.postgres
def test_feed_poll_grants_round_trip(
    poll_privilege_engines: tuple[Engine, Engine],
) -> None:
    admin, _ = poll_privilege_engines
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    try:
        command.downgrade(config, "20261001_04")
        with admin.connect() as connection:
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.feeds', "
                    "'last_attempt_at', 'UPDATE')"
                ),
                {"role": ROLE},
            ).scalar_one()
        command.upgrade(config, "head")
        with admin.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.feeds', "
                    "'last_attempt_at', 'UPDATE')"
                ),
                {"role": ROLE},
            ).scalar_one()
    finally:
        command.upgrade(config, "head")
