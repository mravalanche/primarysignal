"""PostgreSQL integration tests for source and feed creation."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError

from primary_signal.sources.models import Feed, Source
from primary_signal.sources.repository import (
    FeedConflict,
    SourceConflict,
    TransactionalSourceStore,
)


@pytest.fixture
def source_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Migrate an explicitly disposable database and provide its admin engine."""

    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip(
            "set PRIMARY_SIGNAL_TEST_DATABASE_URL and "
            "PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE for PostgreSQL integration tests"
        )

    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()")).scalar_one()
        assert str(database_name).endswith("_test"), (
            "source integration tests require a disposable database ending in _test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    command.upgrade(config, "head")
    try:
        yield engine
    finally:
        engine.dispose()


def _cleanup(engine: Engine, source_ids: list[uuid.UUID]) -> None:
    with engine.begin() as connection:
        connection.execute(delete(Feed).where(Feed.source_id.in_(source_ids)))
        connection.execute(delete(Source).where(Source.id.in_(source_ids)))


@pytest.mark.postgres
def test_source_and_feed_creation_preserve_config_and_use_database_clock(
    source_engine: Engine,
) -> None:
    suffix = uuid.uuid4().hex
    store = TransactionalSourceStore(source_engine)
    with source_engine.connect() as connection:
        before = connection.execute(select(func.clock_timestamp())).scalar_one()

    source = store.create_source(
        source_key=f"source-{suffix}",
        name="  Public Example  ",
        homepage_url="HTTPS://PUBLIC.EXAMPLE:443/",
    )
    submitted = f"HTTPS://PUBLIC.EXAMPLE:443/feed.xml?edition={suffix}"
    feed = store.create_feed(
        source_id=source.id,
        name="  Security feed  ",
        configured_url=submitted,
        poll_interval_seconds=600,
    )

    try:
        with source_engine.connect() as connection:
            after = connection.execute(select(func.clock_timestamp())).scalar_one()
            stored_source = connection.execute(
                select(Source).where(Source.id == source.id)
            ).scalar_one()
            stored_feed = connection.execute(select(Feed).where(Feed.id == feed.id)).scalar_one()

        assert source.name == "Public Example"
        assert source.homepage_url == "https://public.example/"
        assert stored_source.homepage_url == source.homepage_url
        assert feed.name == "Security feed"
        assert feed.configured_url == submitted
        assert feed.normalized_url == f"https://public.example/feed.xml?edition={suffix}"
        assert stored_feed.configured_url == submitted
        assert stored_feed.normalized_url == feed.normalized_url
        assert stored_feed.url_hash == feed.url_hash
        assert before <= feed.next_poll_at <= after
        assert feed.poll_interval_seconds == 600
    finally:
        _cleanup(source_engine, [source.id])


@pytest.mark.postgres
def test_equivalent_feed_identity_and_source_key_conflicts_are_stable(
    source_engine: Engine,
) -> None:
    suffix = uuid.uuid4().hex
    store = TransactionalSourceStore(source_engine)
    source = store.create_source(
        source_key=f"source-{suffix}",
        name="Public Example",
        homepage_url=f"https://public.example/{suffix}",
    )

    try:
        with pytest.raises(SourceConflict, match="source key is already configured"):
            store.create_source(
                source_key=f"source-{suffix}",
                name="Another label",
                homepage_url=f"https://public.example/other/{suffix}",
            )

        store.create_feed(
            source_id=source.id,
            name="Feed",
            configured_url=f"HTTPS://PUBLIC.EXAMPLE:443/{suffix}.xml",
        )
        with pytest.raises(FeedConflict, match="feed URL is already configured") as raised:
            store.create_feed(
                source_id=source.id,
                name="Duplicate",
                configured_url=f"https://public.example/{suffix}.xml",
            )
        assert suffix not in str(raised.value)
    finally:
        _cleanup(source_engine, [source.id])


@pytest.mark.postgres
def test_unknown_database_constraints_are_re_raised(source_engine: Engine) -> None:
    store = TransactionalSourceStore(source_engine)

    with pytest.raises(IntegrityError):
        store.create_feed(
            source_id=uuid.uuid4(),
            name="Orphan feed",
            configured_url=f"https://public.example/{uuid.uuid4().hex}.xml",
        )
