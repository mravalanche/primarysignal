"""Synthetic PostgreSQL proof for retention clocks and protected publications."""

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select, text, update

from primary_signal.ingestion.models import (
    Article,
    ContentVersion,
    ContentVersionRetention,
    FetchAttempt,
)
from primary_signal.ingestion.retention_inventory import list_retention_candidates
from primary_signal.publication.storage import RevisionSource, Story, StoryRevision
from primary_signal.sources.models import Source


@pytest.mark.postgres
def test_retention_clock_and_historical_publication_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not database_url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")
    engine = create_engine(database_url, hide_parameters=True)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", database_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    source_id, article_id = uuid.uuid7(), uuid.uuid7()
    first_id, second_id = uuid.uuid7(), uuid.uuid7()
    now = datetime.now(UTC)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    insert(Source).values(
                        id=source_id,
                        source_key=f"retention-{source_id.hex}",
                        name="Synthetic source",
                        homepage_url="https://public.example/",
                    )
                )
                connection.execute(
                    insert(Article).values(
                        id=article_id,
                        source_id=source_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )

                def add_version(version_id: uuid.UUID, body: str) -> None:
                    attempt_id = uuid.uuid7()
                    connection.execute(
                        insert(FetchAttempt).values(
                            id=attempt_id,
                            article_id=article_id,
                            retrieval_strategy="direct_http",
                            requested_url="https://public.example/notice",
                            redirect_chain=[],
                            status="running",
                            started_at=now,
                        )
                    )
                    connection.execute(
                        insert(ContentVersion).values(
                            id=version_id,
                            article_id=article_id,
                            origin_fetch_attempt_id=attempt_id,
                            raw_response_hash=hashlib.sha256(body.encode()).hexdigest(),
                            normalized_content_hash=hashlib.sha256(body.encode()).hexdigest(),
                            normalization_version=1,
                            extracted_text=body,
                            extractor_name="synthetic-test",
                            extractor_version="1",
                            fetched_at=now,
                        )
                    )

                add_version(first_id, "First synthetic notice")
                add_version(second_id, "Second synthetic notice")
                assert list_retention_candidates(connection) == ()
                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_content_version_id=first_id)
                )
                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_content_version_id=second_id)
                )
                assert connection.execute(
                    select(ContentVersionRetention.content_version_id)
                ).scalars().all() == [first_id]
                old_time = now - timedelta(days=91)
                connection.execute(
                    update(ContentVersionRetention)
                    .where(ContentVersionRetention.content_version_id == first_id)
                    .values(superseded_at=old_time)
                )
                assert [
                    item.content_version_id for item in list_retention_candidates(connection)
                ] == [first_id]

                story_id, revision_id = uuid.uuid7(), uuid.uuid7()
                connection.execute(
                    insert(Story).values(id=story_id, slug=f"retention-{story_id.hex}")
                )
                connection.execute(
                    insert(StoryRevision).values(
                        id=revision_id,
                        story_id=story_id,
                        revision_number=1,
                        headline="Synthetic notice",
                        synthesis="A synthetic summary.",
                        why_it_matters="A synthetic reason.",
                        primary_topic="security-engineering",
                        story_type="news",
                        first_reported_at=now,
                        latest_material_update_at=now,
                    )
                )
                connection.execute(
                    insert(RevisionSource).values(
                        revision_id=revision_id,
                        source_id="synthetic-source",
                        position=1,
                        title="Synthetic notice",
                        publisher="Synthetic source",
                        public_url="https://public.example/notice",
                        article_id=article_id,
                        content_version_id=first_id,
                    )
                )
                connection.execute(
                    update(StoryRevision)
                    .where(StoryRevision.id == revision_id)
                    .values(status="validated")
                )
                connection.execute(
                    update(StoryRevision)
                    .where(StoryRevision.id == revision_id)
                    .values(status="published", published_at=now)
                )
                assert list_retention_candidates(connection) == ()
                connection.execute(
                    update(StoryRevision)
                    .where(StoryRevision.id == revision_id)
                    .values(status="suppressed")
                )
                assert list_retention_candidates(connection) == ()

                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_content_version_id=first_id)
                )
                assert connection.execute(
                    select(ContentVersionRetention.content_version_id)
                ).scalars().all() == [second_id]
                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_content_version_id=second_id)
                )
                reset_time = connection.execute(
                    select(ContentVersionRetention.superseded_at).where(
                        ContentVersionRetention.content_version_id == first_id
                    )
                ).scalar_one()
                assert reset_time > old_time
                assert list_retention_candidates(connection) == ()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_inventory_rejects_unbounded_page() -> None:
    for limit in (0, 501):
        with pytest.raises(ValueError, match="between 1 and 500"):
            list_retention_candidates(None, limit=limit)  # type: ignore[arg-type]
