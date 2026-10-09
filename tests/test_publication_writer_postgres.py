"""Disposable PostgreSQL proof of atomic publication and writer privileges."""

import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from primary_signal.publication.decision_writer import PublicationDecisionWriter
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
    decision_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DECISION_DATABASE_URL")
    decision_engine = create_engine(decision_url, hide_parameters=True) if decision_url else None
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
    with writer_engine.begin() as connection, pytest.raises(DBAPIError) as denied_story:
        connection.execute(text("UPDATE primary_signal.stories SET suppressed=true"))
    assert getattr(denied_story.value.orig, "sqlstate", None) == "42501"
    with writer_engine.begin() as connection, pytest.raises(DBAPIError) as denied_revision:
        connection.execute(text("UPDATE primary_signal.story_revisions SET status='validated'"))
    assert getattr(denied_revision.value.orig, "sqlstate", None) == "42501"
    with writer_engine.begin() as connection, pytest.raises(DBAPIError) as denied_event:
        connection.execute(
            text(
                "INSERT INTO primary_signal.publication_events "
                "(id,story_id,revision_id,to_status,actor,reason) "
                "VALUES (:id,:story,:revision,'draft','test-editor','bypass')"
            ),
            {"id": uuid.uuid7(), "story": uuid.uuid7(), "revision": uuid.uuid7()},
        )
    assert getattr(denied_event.value.orig, "sqlstate", None) == "42501"
    writer = PublicationWriter(writer_engine, expected_role=PUBLICATION_ROLE)
    decision_writer = (
        PublicationDecisionWriter(decision_engine, expected_role="publication_decision_test")
        if decision_engine is not None
        else writer
    )
    now = datetime.now(UTC)
    source_id, article_id, article_url_id, attempt_id, version_id = (uuid.uuid7() for _ in range(5))
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
                "INSERT INTO primary_signal.article_urls "
                "(id,article_id,original_url,normalized_url,normalized_url_hash,"
                "normalization_version,kind,first_seen_at,last_seen_at) "
                "VALUES (:id,:article,:url,:url,:hash,1,'submitted',:now,:now)"
            ),
            {
                "id": article_url_id,
                "article": article_id,
                "url": "https://public.example/notice",
                "hash": hashlib.sha256(b"https://public.example/notice").hexdigest(),
                "now": now,
            },
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
    with writer_engine.begin() as connection, pytest.raises(DBAPIError) as frozen_source:
        connection.execute(
            text(
                "INSERT INTO primary_signal.revision_sources "
                "(revision_id,source_id,position,title,publisher,public_url,article_id,"
                "content_version_id) VALUES (:revision,'second-source',2,'Second source',"
                "'Public Example','https://public.example/notice',:article,:version)"
            ),
            {"revision": first_id, "article": article_id, "version": version_id},
        )
    assert getattr(frozen_source.value.orig, "sqlstate", None) == "P0001"
    start = threading.Barrier(2)

    def concurrent_publish() -> str:
        start.wait(timeout=5)
        try:
            writer.publish_reviewed(
                story_id=story_id,
                revision_id=first_id,
                input_fingerprint=fingerprint,
                expected_current_revision_id=None,
                decision=decision,
            )
        except PublicationConflict:
            return "conflict"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(concurrent_publish) for _ in range(2)]
        assert sorted(attempt.result(timeout=15) for attempt in attempts) == [
            "conflict",
            "published",
        ]
    with admin.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM primary_signal.publication_events WHERE story_id=:story"
                ),
                {"story": story_id},
            ).scalar_one()
            == 3
        )
    with pytest.raises(PublicationConflict, match="conflicts with stored state"):
        writer.suppress(
            story_id=story_id,
            expected_current_revision_id=uuid.uuid7(),
            decision=decision,
        )
    _, successor_id, successor_fingerprint = writer.create_draft(
        draft("Successor version"), reference
    )
    with pytest.raises(PublicationConflict, match="conflicts with stored state"):
        writer.publish_reviewed(
            story_id=story_id,
            revision_id=successor_id,
            input_fingerprint="0" * 64,
            expected_current_revision_id=first_id,
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
    decision_writer.publish_reviewed(
        story_id=story_id,
        revision_id=successor_id,
        input_fingerprint=successor_fingerprint,
        expected_current_revision_id=first_id,
        decision=decision,
    )
    with pytest.raises(PublicationConflict, match="conflicts with stored state"):
        writer.suppress(
            story_id=story_id,
            expected_current_revision_id=first_id,
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
    decision_writer.suppress(
        story_id=story_id, expected_current_revision_id=successor_id, decision=decision
    )
    with admin.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM primary_signal_public.stories WHERE story_id=:id"),
                {"id": story_id},
            ).scalar_one()
            == 0
        )
    lock_story = replace(draft("Locked source notice"), slug=f"synthetic-lock-{uuid.uuid7().hex}")
    lock_story_id, lock_revision_id, lock_fingerprint = writer.create_draft(lock_story, reference)
    with admin.connect() as holding:
        holding.execute(
            text("SELECT id FROM primary_signal.content_versions WHERE id=:id FOR UPDATE"),
            {"id": version_id},
        )
        with (
            (decision_engine or writer_engine).begin() as blocked,
            pytest.raises(DBAPIError) as waiting,
        ):
            blocked.execute(text("SET LOCAL lock_timeout = '250ms'"))
            blocked.execute(
                text(
                    "SELECT primary_signal.publish_reviewed("
                    ":story,:revision,:fingerprint,NULL,:actor,:reason)"
                ),
                {
                    "story": lock_story_id,
                    "revision": lock_revision_id,
                    "fingerprint": lock_fingerprint,
                    "actor": "test-editor",
                    "reason": "Reviewed synthetic evidence",
                },
            )
        assert getattr(waiting.value.orig, "sqlstate", None) == "55P03"
        holding.rollback()
    decision_writer.publish_reviewed(
        story_id=lock_story_id,
        revision_id=lock_revision_id,
        input_fingerprint=lock_fingerprint,
        expected_current_revision_id=None,
        decision=decision,
    )
    with admin.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text("UPDATE primary_signal.content_versions SET extracted_text=NULL WHERE id=:id"),
            {"id": version_id},
        )
    cleared_attempt, cleared_version = uuid.uuid7(), uuid.uuid7()
    with admin.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO primary_signal.fetch_attempts "
                "(id,article_id,retrieval_strategy,requested_url,redirect_chain,status,started_at) "
                "VALUES (:id,:article,'direct','https://public.example/notice',"
                "'[]'::jsonb,'running',:now)"
            ),
            {"id": cleared_attempt, "article": article_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.content_versions "
                "(id,article_id,origin_fetch_attempt_id,raw_response_hash,"
                "normalized_content_hash,normalization_version,extracted_text,"
                "extractor_name,extractor_version,fetched_at) VALUES "
                "(:id,:article,:attempt,:raw,:normal,1,'Unpublished synthetic evidence',"
                "'synthetic','1',:now)"
            ),
            {
                "id": cleared_version,
                "article": article_id,
                "attempt": cleared_attempt,
                "raw": "c" * 64,
                "normal": "d" * 64,
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE primary_signal.fetch_attempts SET status='fetched',"
                "completed_at=:now,resulting_content_version_id=:version WHERE id=:id"
            ),
            {"id": cleared_attempt, "now": now, "version": cleared_version},
        )
    cleared_story = replace(
        draft("Cleared source notice"), slug=f"synthetic-cleared-{uuid.uuid7().hex}"
    )
    cleared_story_id, cleared_revision_id, cleared_fingerprint = writer.create_draft(
        cleared_story, (DraftReference(source, article_id, cleared_version),)
    )
    # Keep the synthetic cleared row in a transaction so migration round-trip
    # tests can still restore the original NOT NULL constraint.
    with admin.connect() as connection:
        connection.execute(
            text("UPDATE primary_signal.content_versions SET extracted_text=NULL WHERE id=:id"),
            {"id": cleared_version},
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                text(
                    "SELECT primary_signal.publish_reviewed("
                    ":story,:revision,:fingerprint,NULL,:actor,:reason)"
                ),
                {
                    "story": cleared_story_id,
                    "revision": cleared_revision_id,
                    "fingerprint": cleared_fingerprint,
                    "actor": "test-editor",
                    "reason": "Reviewed synthetic evidence",
                },
            )
        assert (
            connection.execute(
                text("SELECT current_revision_id FROM primary_signal.stories WHERE id=:id"),
                {"id": cleared_story_id},
            ).scalar_one_or_none()
            is None
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM primary_signal.publication_events WHERE story_id=:id"),
                {"id": cleared_story_id},
            ).scalar_one()
            == 1
        )
        connection.rollback()
    unlinked = replace(source, url="https://public.example/unlinked")
    unlinked_story = replace(
        draft("Unlinked notice"),
        slug=f"synthetic-unlinked-{uuid.uuid7().hex}",
        sources=(unlinked,),
    )
    unlinked_story_id, unlinked_revision_id, unlinked_fingerprint = writer.create_draft(
        unlinked_story, (DraftReference(unlinked, article_id, version_id),)
    )
    with pytest.raises(PublicationConflict, match="conflicts with stored state"):
        writer.publish_reviewed(
            story_id=unlinked_story_id,
            revision_id=unlinked_revision_id,
            input_fingerprint=unlinked_fingerprint,
            expected_current_revision_id=None,
            decision=decision,
        )
    with admin.connect() as connection:
        assert (
            connection.execute(
                text("SELECT current_revision_id FROM primary_signal.stories WHERE id=:id"),
                {"id": unlinked_story_id},
            ).scalar_one_or_none()
            is None
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM primary_signal.publication_events WHERE story_id=:id"),
                {"id": unlinked_story_id},
            ).scalar_one()
            == 1
        )
    with admin.begin() as connection:
        connection.execute(
            text("UPDATE primary_signal.sources SET enabled=false WHERE id=:id"),
            {"id": source_id},
        )
    disabled_story = replace(
        draft("Disabled source"), slug=f"synthetic-disabled-{uuid.uuid7().hex}"
    )
    disabled_story_id, disabled_revision_id, disabled_fingerprint = writer.create_draft(
        disabled_story, reference
    )
    with pytest.raises(PublicationConflict, match="conflicts with stored state"):
        writer.publish_reviewed(
            story_id=disabled_story_id,
            revision_id=disabled_revision_id,
            input_fingerprint=disabled_fingerprint,
            expected_current_revision_id=None,
            decision=decision,
        )
    with admin.connect() as connection:
        assert (
            connection.execute(
                text("SELECT current_revision_id FROM primary_signal.stories WHERE id=:id"),
                {"id": disabled_story_id},
            ).scalar_one_or_none()
            is None
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM primary_signal.publication_events WHERE story_id=:id"),
                {"id": disabled_story_id},
            ).scalar_one()
            == 1
        )
    writer_engine.dispose()
    if decision_engine is not None:
        decision_engine.dispose()
    public_engine.dispose()
    admin.dispose()
