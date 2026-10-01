import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError


def assert_history_and_parent_constraints(connection: Connection) -> None:
    """Exercise the PostgreSQL-only constraints that protect provenance."""

    source_id = uuid.uuid7()
    first_feed_id = uuid.uuid7()
    second_feed_id = uuid.uuid7()
    poll_id = uuid.uuid7()
    connection.execute(
        text(
            "INSERT INTO primary_signal.sources "
            "(id, source_key, name, homepage_url) "
            "VALUES (:id, 'example-source', 'Example Source', 'https://source.public.example')"
        ),
        {"id": source_id},
    )
    for feed_id, name, suffix in (
        (first_feed_id, "First feed", "first"),
        (second_feed_id, "Second feed", "second"),
    ):
        connection.execute(
            text(
                "INSERT INTO primary_signal.feeds "
                "(id, source_id, name, configured_url, normalized_url, url_hash, "
                "url_normalization_version) VALUES "
                "(:id, :source_id, :name, :url, :url, :hash, 1)"
            ),
            {
                "id": feed_id,
                "source_id": source_id,
                "name": name,
                "url": f"https://{suffix}.public.example/feed",
                "hash": suffix.ljust(64, "0"),
            },
        )
    connection.execute(
        text(
            "INSERT INTO primary_signal.feed_poll_runs "
            "(id, feed_id, requested_url, started_at) "
            "VALUES (:id, :feed_id, 'https://first.public.example/feed', now())"
        ),
        {"id": poll_id, "feed_id": first_feed_id},
    )

    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            text("DELETE FROM primary_signal.feed_poll_runs WHERE id=:id"), {"id": poll_id}
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text(
                "INSERT INTO primary_signal.feed_entries "
                "(id, feed_id, first_poll_run_id, identity_method, identity_version, "
                "identity_key, metadata_hash, first_seen_at, last_seen_at) VALUES "
                "(:id, :feed_id, :poll_id, 'guid', 1, :identity, :hash, now(), now())"
            ),
            {
                "id": uuid.uuid7(),
                "feed_id": second_feed_id,
                "poll_id": poll_id,
                "identity": "b" * 64,
                "hash": "a" * 64,
            },
        )

    connection.execute(
        text(
            "UPDATE primary_signal.feed_poll_runs "
            "SET status='succeeded', completed_at=now() WHERE id=:id"
        ),
        {"id": poll_id},
    )
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            text("UPDATE primary_signal.feed_poll_runs SET error_code='changed' WHERE id=:id"),
            {"id": poll_id},
        )


@pytest.mark.postgres
def test_blank_postgresql_database_migrates_to_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise PostgreSQL-only DDL when a disposable test database is explicit."""

    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip(
            "set PRIMARY_SIGNAL_TEST_DATABASE_URL and "
            "PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE for PostgreSQL integration tests"
        )

    engine = create_engine(url)
    with engine.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()"))
        assert str(database_name.scalar_one()).endswith("_test"), (
            "migration integration tests require a disposable database ending in _test"
        )
        connection.execute(text("DROP SCHEMA IF EXISTS primary_signal CASCADE"))
        connection.commit()

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    repository_root = Path(__file__).resolve().parents[1]
    config = Config(repository_root / "alembic.ini")

    try:
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names(schema="primary_signal"))
        assert {
            "articles",
            "content_versions",
            "feeds",
            "jobs",
            "sources",
        } <= table_names

        with engine.connect() as connection:
            transaction = connection.begin()
            assert_history_and_parent_constraints(connection)
            transaction.rollback()

        command.upgrade(config, "head")
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert inspect(connection).get_table_names(schema="primary_signal") == []
        command.upgrade(config, "head")
    finally:
        with engine.connect() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS primary_signal CASCADE"))
            connection.commit()
        engine.dispose()
