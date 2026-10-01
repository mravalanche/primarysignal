"""Unit tests for validated source and feed creation."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from primary_signal.identity.urls import identify_url
from primary_signal.sources.repository import (
    FeedConflict,
    FeedRecord,
    SourceConflict,
    SourceFeedRepository,
    SourceRecord,
    TransactionalSourceStore,
)


def _source_row() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_key="example-source",
        name="Example Source",
        homepage_url="https://public.example/",
        enabled=True,
        created_at=now,
        updated_at=now,
    )


def _feed_row(source_id: uuid.UUID, configured_url: str) -> SimpleNamespace:
    now = datetime.now(UTC)
    identity = identify_url(configured_url)
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_id=source_id,
        name="Security feed",
        configured_url=configured_url,
        normalized_url=identity.normalized_url,
        url_hash=identity.url_hash,
        url_normalization_version=identity.normalization_version,
        enabled=True,
        poll_interval_seconds=900,
        next_poll_at=now,
        created_at=now,
        updated_at=now,
    )


def _connection_with_row(row: SimpleNamespace) -> MagicMock:
    connection = MagicMock()
    connection.execute.return_value.one.return_value = row
    return connection


def _integrity_error(constraint_name: str | None) -> IntegrityError:
    class OriginalError(Exception):
        diag: SimpleNamespace

        def __init__(self) -> None:
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    diagnostic = SimpleNamespace(constraint_name=constraint_name)
    original = OriginalError()
    original.diag = diagnostic
    return IntegrityError("statement", {}, original)


def test_create_source_normalizes_homepage_and_trims_name() -> None:
    row = _source_row()
    connection = _connection_with_row(row)
    repository = SourceFeedRepository(cast(Connection, connection))

    record = repository.create_source(
        source_key="example-source",
        name="  Example Source  ",
        homepage_url="HTTPS://PUBLIC.EXAMPLE:443/",
    )

    assert record == SourceRecord(**vars(row))
    statement = connection.execute.call_args.args[0]
    parameters = statement.compile(dialect=postgresql.dialect()).params
    assert parameters["source_key"] == "example-source"
    assert parameters["name"] == "Example Source"
    assert parameters["homepage_url"] == "https://public.example/"


@pytest.mark.parametrize("source_key", ["", "Example", "example_source", "-example", "x" * 101])
def test_create_source_rejects_invalid_keys_before_sql(source_key: str) -> None:
    connection = MagicMock()

    with pytest.raises(ValueError, match="source key"):
        SourceFeedRepository(cast(Connection, connection)).create_source(
            source_key=source_key,
            name="Example",
            homepage_url="https://public.example/",
        )

    connection.execute.assert_not_called()


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_create_source_rejects_invalid_names_before_sql(name: str) -> None:
    connection = MagicMock()

    with pytest.raises(ValueError, match="name"):
        SourceFeedRepository(cast(Connection, connection)).create_source(
            source_key="example",
            name=name,
            homepage_url="https://public.example/",
        )

    connection.execute.assert_not_called()


def test_create_source_validates_url_and_boolean_before_sql() -> None:
    connection = MagicMock()
    repository = SourceFeedRepository(cast(Connection, connection))

    with pytest.raises(ValueError, match="supported, well-formed"):
        repository.create_source(source_key="example", name="Example", homepage_url="file:///tmp")
    with pytest.raises(ValueError, match="enabled"):
        repository.create_source(
            source_key="example",
            name="Example",
            homepage_url="https://public.example/",
            enabled=cast(bool, 1),
        )

    connection.execute.assert_not_called()


def test_create_feed_preserves_configured_url_and_derives_identity() -> None:
    source_id = uuid.uuid4()
    submitted = "HTTPS://PUBLIC.EXAMPLE:443/feed.xml?edition=uk"
    row = _feed_row(source_id, submitted)
    connection = _connection_with_row(row)

    record = SourceFeedRepository(cast(Connection, connection)).create_feed(
        source_id=source_id,
        name="  Security feed  ",
        configured_url=submitted,
    )

    assert record == FeedRecord(**vars(row))
    statement = connection.execute.call_args.args[0]
    parameters = statement.compile(dialect=postgresql.dialect()).params
    identity = identify_url(submitted)
    assert parameters["configured_url"] == submitted
    assert parameters["normalized_url"] == identity.normalized_url
    assert parameters["url_hash"] == identity.url_hash
    assert parameters["url_normalization_version"] == identity.normalization_version
    assert "clock_timestamp()" in str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("interval", [59, 2_592_001, True])
def test_create_feed_rejects_invalid_intervals_before_sql(interval: int) -> None:
    connection = MagicMock()

    with pytest.raises(ValueError, match="poll interval"):
        SourceFeedRepository(cast(Connection, connection)).create_feed(
            source_id=uuid.uuid4(),
            name="Feed",
            configured_url="https://public.example/feed.xml",
            poll_interval_seconds=interval,
        )

    connection.execute.assert_not_called()


def test_create_feed_validates_source_id_name_url_and_boolean_before_sql() -> None:
    connection = MagicMock()
    repository = SourceFeedRepository(cast(Connection, connection))

    with pytest.raises(ValueError, match="source id"):
        repository.create_feed(
            source_id=cast(uuid.UUID, "not-a-uuid"),
            name="Feed",
            configured_url="https://public.example/feed.xml",
        )
    with pytest.raises(ValueError, match="name"):
        repository.create_feed(
            source_id=uuid.uuid4(), name=" ", configured_url="https://public.example/feed.xml"
        )
    with pytest.raises(ValueError, match="supported, well-formed"):
        repository.create_feed(
            source_id=uuid.uuid4(), name="Feed", configured_url="https://public.example/%"
        )
    with pytest.raises(ValueError, match="enabled"):
        repository.create_feed(
            source_id=uuid.uuid4(),
            name="Feed",
            configured_url="https://public.example/feed.xml",
            enabled=cast(bool, 1),
        )

    connection.execute.assert_not_called()


def test_known_unique_constraints_have_stable_conflicts_without_submitted_values() -> None:
    source_connection = MagicMock()
    source_connection.execute.side_effect = _integrity_error("uq_sources_source_key")
    with pytest.raises(SourceConflict) as source_error:
        SourceFeedRepository(cast(Connection, source_connection)).create_source(
            source_key="private-looking-source",
            name="Example",
            homepage_url="https://public.example/",
        )
    assert "private-looking-source" not in str(source_error.value)

    feed_connection = MagicMock()
    feed_connection.execute.side_effect = _integrity_error(
        "uq_feeds_url_normalization_version_url_hash"
    )
    submitted = "https://public.example/feed.xml?token=REDACTED"
    with pytest.raises(FeedConflict) as feed_error:
        SourceFeedRepository(cast(Connection, feed_connection)).create_feed(
            source_id=uuid.uuid4(), name="Feed", configured_url=submitted
        )
    assert submitted not in str(feed_error.value)
    assert "REDACTED" not in str(feed_error.value)


def test_unknown_integrity_errors_are_not_reclassified() -> None:
    connection = MagicMock()
    error = _integrity_error("fk_feeds_source_id_sources")
    connection.execute.side_effect = error

    with pytest.raises(IntegrityError) as raised:
        SourceFeedRepository(cast(Connection, connection)).create_feed(
            source_id=uuid.uuid4(),
            name="Feed",
            configured_url="https://public.example/feed.xml",
        )

    assert raised.value is error


def test_transactional_store_opens_one_transaction_per_operation() -> None:
    engine = MagicMock(spec=Engine)
    connection = MagicMock(spec=Connection)
    engine.begin.return_value.__enter__.return_value = connection
    source_record = SourceRecord(**vars(_source_row()))
    feed_row = _feed_row(source_record.id, "https://public.example/feed.xml")
    feed_record = FeedRecord(**vars(feed_row))
    repositories: list[MagicMock] = []

    def factory(received: Connection) -> MagicMock:
        assert received is connection
        repository = MagicMock()
        repository.create_source.return_value = source_record
        repository.create_feed.return_value = feed_record
        repositories.append(repository)
        return repository

    store = TransactionalSourceStore(engine, repository_factory=factory)

    assert (
        store.create_source(
            source_key="example-source",
            name="Example Source",
            homepage_url="https://public.example/",
        )
        == source_record
    )
    assert (
        store.create_feed(
            source_id=source_record.id,
            name="Feed",
            configured_url="https://public.example/feed.xml",
        )
        == feed_record
    )
    assert engine.begin.call_count == 2
    assert len(repositories) == 2


def test_records_are_frozen() -> None:
    record = SourceRecord(**vars(_source_row()))

    with pytest.raises(AttributeError):
        record.name = "Changed"  # type: ignore[misc]


def test_record_representations_do_not_expose_urls_or_hashes() -> None:
    source = SourceRecord(**vars(_source_row()))
    feed_row = _feed_row(source.id, "https://public.example/feed.xml?token=REDACTED")
    feed = FeedRecord(**vars(feed_row))

    assert source.homepage_url not in repr(source)
    assert feed.configured_url not in repr(feed)
    assert feed.normalized_url not in repr(feed)
    assert feed.url_hash not in repr(feed)
