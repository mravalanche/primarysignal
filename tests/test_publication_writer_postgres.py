"""Disposable PostgreSQL proof of atomic publication and writer privileges."""

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from scripts.bootstrap_test_database_roles import (
    PUBLIC_PASSWORD,
    PUBLIC_ROLE,
    PUBLICATION_PASSWORD,
    PUBLICATION_ROLE,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from primary_signal.entrypoints.web import assert_public_database_role
from primary_signal.publication.models import PublicSource, PublicStory, StoryType, Topic
from primary_signal.publication.writer import (
    DraftReference,
    OperatorDecision,
    PublicationConflict,
    PublicationWriter,
    assert_publication_writer_role,
)


@pytest.mark.postgres
def test_writer_role_and_successor_atomicity(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip("configure the disposable PostgreSQL test database")
    admin = create_engine(url, hide_parameters=True)
    with admin.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    role_url = make_url(url).set(username=PUBLICATION_ROLE, password=PUBLICATION_PASSWORD)
    writer_engine = create_engine(role_url, hide_parameters=True)
    public_url = make_url(url).set(username=PUBLIC_ROLE, password=PUBLIC_PASSWORD)
    public_engine = create_engine(public_url, hide_parameters=True)
    with writer_engine.connect() as connection:
        assert_publication_writer_role(connection)
        with pytest.raises(RuntimeError, match="public database role"):
            assert_public_database_role(connection)
    with public_engine.connect() as connection:
        assert_public_database_role(connection)
        with pytest.raises(RuntimeError, match="publication writer role"):
            assert_publication_writer_role(connection)
    writer = PublicationWriter(writer_engine, expected_role=PUBLICATION_ROLE)
    now = datetime.now(UTC)
    source_id, article_id, attempt_id, version_id = (uuid.uuid7() for _ in range(4))
    with admin.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO primary_signal.sources(id,source_key,name,homepage_url) "
                "VALUES (:id,:key,'Synthetic source','https://public.example/')"
            ),
            {"id": source_id, "key": f"publication-{source_id.hex}"},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.articles(id,source_id,first_seen_at,last_seen_at) "
                "VALUES (:id,:source,:now,:now)"
            ),
            {"id": article_id, "source": source_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.fetch_attempts "
                "(id,article_id,retrieval_strategy,requested_url,redirect_chain,status,"
                "started_at) VALUES (:id,:article,'direct','https://public.example/notice',"
                "'[]'::jsonb,'running',:now)"
            ),
            {"id": attempt_id, "article": article_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.content_versions "
                "(id,article_id,origin_fetch_attempt_id,raw_response_hash,"
                "normalized_content_hash,normalization_version,extracted_text,"
                "extractor_name,extractor_version,fetched_at) VALUES "
                "(:id,:article,:attempt,:raw,:normal,1,'Synthetic evidence',"
                "'synthetic','1',:now)"
            ),
            {
                "id": version_id,
                "article": article_id,
                "attempt": attempt_id,
                "raw": "a" * 64,
                "normal": "b" * 64,
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE primary_signal.fetch_attempts SET status='fetched',"
                "completed_at=:now,resulting_content_version_id=:version "
                "WHERE id=:id"
            ),
            {"id": attempt_id, "now": now, "version": version_id},
        )
    slug = f"synthetic-publication-{uuid.uuid7().hex}"
    source = PublicSource(
        id="source-one",
        title="Synthetic source",
        publisher="Public Example",
        url="https://public.example/notice",
        first_published_at=now,
        is_primary=True,
    )

    def draft(headline: str) -> PublicStory:
        return PublicStory(
            slug=slug,
            headline=headline,
            synthesis="Synthetic summary",
            why_it_matters="Synthetic relevance",
            primary_topic=Topic.SECURITY_ENGINEERING,
            story_type=StoryType.ADVISORY,
            first_reported_at=now,
            latest_material_update_at=now,
            source_count=1,
            sources=(source,),
        )

    reference = (DraftReference(source, article_id, version_id),)
    decision = OperatorDecision(actor="test-editor", reason="Reviewed synthetic evidence")
    story_id, first_id, fingerprint = writer.create_draft(draft("First version"), reference)
    writer.publish_reviewed(
        story_id=story_id, revision_id=first_id, input_fingerprint=fingerprint, decision=decision
    )
    _, successor_id, successor_fingerprint = writer.create_draft(
        draft("Successor version"), reference
    )
    with pytest.raises(PublicationConflict, match="fingerprint changed"):
        writer.publish_reviewed(
            story_id=story_id,
            revision_id=successor_id,
            input_fingerprint="0" * 64,
            decision=decision,
        )
    with admin.connect() as connection:
        assert (
            connection.execute(
                text("SELECT current_revision_id FROM primary_signal.stories WHERE id=:id"),
                {"id": story_id},
            ).scalar_one()
            == first_id
        )
    writer.publish_reviewed(
        story_id=story_id,
        revision_id=successor_id,
        input_fingerprint=successor_fingerprint,
        decision=decision,
    )
    with admin.connect() as connection:
        assert (
            connection.execute(
                text("SELECT status FROM primary_signal.story_revisions WHERE id=:id"),
                {"id": first_id},
            ).scalar_one()
            == "superseded"
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM primary_signal.publication_events "
                    "WHERE story_id=:id AND actor='test-editor'"
                ),
                {"id": story_id},
            ).scalar_one()
            == 5
        )
    with writer_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(text("SELECT extracted_text FROM primary_signal.content_versions"))
    writer.suppress(story_id=story_id, decision=decision)
    with admin.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM primary_signal_public.stories WHERE story_id=:id"),
                {"id": story_id},
            ).scalar_one()
            == 0
        )
    writer_engine.dispose()
    public_engine.dispose()
    admin.dispose()
