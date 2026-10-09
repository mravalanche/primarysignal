"""Disposable PostgreSQL proof of the bounded text-only maintenance capability."""

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.entrypoints.retention_cleanup import run_batch
from primary_signal.ingestion.retention_cleanup import clear_expired_extracted_text
from primary_signal.publication.decision_writer import PublicationDecisionWriter
from primary_signal.publication.models import PublicSource, PublicStory, StoryType, Topic
from primary_signal.publication.writer import (
    DraftReference,
    OperatorDecision,
    PublicationWriter,
)


def _insert_candidate(connection: Connection, *, age_days: int) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    source_id, article_id, old_id, current_id = (uuid.uuid7() for _ in range(4))
    article_url = f"https://public.example/notice/{article_id}"
    connection.execute(
        text(
            "INSERT INTO primary_signal.sources(id,source_key,name,homepage_url) "
            "VALUES (:id,:key,'Synthetic source','https://public.example/')"
        ),
        {"id": source_id, "key": f"cleanup-{source_id.hex}"},
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
            "VALUES (:id,:article,:url,:url,:digest,1,'submitted',:now,:now)"
        ),
        {
            "id": uuid.uuid7(),
            "article": article_id,
            "url": article_url,
            "digest": hashlib.sha256(article_url.encode()).hexdigest(),
            "now": now,
        },
    )
    for version_id in (old_id, current_id):
        attempt_id = uuid.uuid7()
        body = f"Synthetic body {version_id}"
        digest = hashlib.sha256(body.encode()).hexdigest()
        connection.execute(
            text(
                "INSERT INTO primary_signal.fetch_attempts "
                "(id,article_id,retrieval_strategy,requested_url,redirect_chain,status,started_at) "
                "VALUES (:id,:article,'direct_http',:url,"
                "'[]'::jsonb,'running',:now)"
            ),
            {"id": attempt_id, "article": article_id, "url": article_url, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.content_versions "
                "(id,article_id,origin_fetch_attempt_id,raw_response_hash,"
                "normalized_content_hash,normalization_version,extracted_text,"
                "extractor_name,extractor_version,fetched_at) "
                "VALUES (:id,:article,:attempt,:digest,:digest,1,:body,'synthetic','1',:now)"
            ),
            {
                "id": version_id,
                "article": article_id,
                "attempt": attempt_id,
                "digest": digest,
                "body": body,
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE primary_signal.fetch_attempts "
                "SET status='fetched',completed_at=:now,resulting_content_version_id=:version "
                "WHERE id=:attempt"
            ),
            {"now": now, "version": version_id, "attempt": attempt_id},
        )
    for version_id in (old_id, current_id):
        connection.execute(
            text(
                "UPDATE primary_signal.articles SET current_content_version_id=:version "
                "WHERE id=:article"
            ),
            {"version": version_id, "article": article_id},
        )
    connection.execute(
        text(
            "UPDATE primary_signal.content_version_retention SET superseded_at=:old "
            "WHERE content_version_id=:id"
        ),
        {"old": now - timedelta(days=age_days), "id": old_id},
    )
    return article_id, old_id


def _create_draft(
    writer: PublicationWriter, article_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, str]:
    now = datetime.now(UTC)
    source = PublicSource(
        id="synthetic-source",
        title="Synthetic notice",
        publisher="Synthetic source",
        url=f"https://public.example/notice/{article_id}",
        first_published_at=now,
        is_primary=True,
    )
    story = PublicStory(
        slug=f"retention-{uuid.uuid7().hex}",
        headline="Synthetic notice",
        synthesis="Synthetic summary",
        why_it_matters="Synthetic relevance",
        primary_topic=Topic.SECURITY_ENGINEERING,
        story_type=StoryType.ADVISORY,
        first_reported_at=now,
        latest_material_update_at=now,
        source_count=1,
        sources=(source,),
    )
    return writer.create_draft(story, (DraftReference(source, article_id, version_id),))


@pytest.mark.postgres
def test_cleanup_clears_only_expired_unpublished_text(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    role_url = os.environ.get("PRIMARY_SIGNAL_TEST_RETENTION_CLEANUP_DATABASE_URL")
    writer_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_URL")
    decision_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DECISION_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not admin_url or not role_url or not writer_url or not decision_url or not expected_role:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires restricted retention cleanup DSN")
        pytest.skip("set disposable PostgreSQL test database settings")
    admin = create_engine(admin_url, hide_parameters=True)
    maintenance = create_engine(role_url, hide_parameters=True)
    writer_engine = create_engine(writer_url, hide_parameters=True)
    decision_engine = create_engine(decision_url, hide_parameters=True)
    with admin.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        with admin.begin() as connection:
            old_article, old_id = _insert_candidate(connection, age_days=91)
            _, recent_id = _insert_candidate(connection, age_days=89)
            protected_article, protected_id = _insert_candidate(connection, age_days=91)
            race_article, race_id = _insert_candidate(connection, age_days=91)
        writer = PublicationWriter(writer_engine, expected_role="publication_test")
        decider = PublicationDecisionWriter(
            decision_engine, expected_role="publication_decision_test"
        )
        old_story, old_revision, old_fingerprint = _create_draft(writer, old_article, old_id)
        protected_story, protected_revision, protected_fingerprint = _create_draft(
            writer, protected_article, protected_id
        )
        decision = OperatorDecision(actor="test-editor", reason="Reviewed synthetic evidence")
        decider.publish_reviewed(
            story_id=protected_story,
            revision_id=protected_revision,
            input_fingerprint=protected_fingerprint,
            expected_current_revision_id=None,
            decision=decision,
        )
        decider.suppress(
            story_id=protected_story,
            expected_current_revision_id=protected_revision,
            decision=decision,
        )
        with maintenance.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                "retention_cleanup_test"
            )
        with admin.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT has_function_privilege('primary_signal_cap_retention_cleanup',"
                    "'primary_signal.clear_expired_extracted_text(integer)','EXECUTE')"
                )
            ).scalar_one()
            for role in ("processor_test", "public_test", "publication_decision_test"):
                assert not connection.execute(
                    text(
                        "SELECT has_function_privilege(:role,"
                        "'primary_signal.clear_expired_extracted_text(integer)','EXECUTE')"
                    ),
                    {"role": role},
                ).scalar_one()
            for table_name in ("content_versions", "articles", "story_revisions"):
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege('retention_cleanup_test',"
                        "'primary_signal.' || :table_name,'SELECT,UPDATE,DELETE')"
                    ),
                    {"table_name": table_name},
                ).scalar_one()
        for statement in (
            "SELECT extracted_text FROM primary_signal.content_versions WHERE false",
            "UPDATE primary_signal.content_versions SET extracted_text=NULL WHERE false",
            "SELECT id FROM primary_signal.articles WHERE false",
            "SELECT primary_signal.find_text_bearing_content_version("
            "'00000000-0000-0000-0000-000000000000',1,'x')",
        ):
            with pytest.raises(DBAPIError), maintenance.begin() as connection:
                connection.execute(text(statement))
        for limit in (0, 501):
            with (
                pytest.raises(ValueError, match="between 1 and 500"),
                maintenance.begin() as connection,
            ):
                clear_expired_extracted_text(connection, limit=limit)
        # Keep the cleared row inside a transaction that rolls back. Migration
        # round-trip tests later in the same CI database must still be able to
        # restore the former NOT NULL constraint.
        with admin.connect() as clearing:
            clearing.execute(text("SET LOCAL ROLE retention_cleanup_test"))
            # Use SQL here because the application wrapper correctly rejects
            # SET ROLE impersonation (session_user differs from current_user).
            assert tuple(
                clearing.execute(
                    text("SELECT primary_signal.clear_expired_extracted_text(1)")
                ).scalars()
            ) == (old_id,)
            clearing.execute(text("RESET ROLE"))
            assert clearing.execute(
                text(
                    "SELECT extracted_text IS NULL FROM primary_signal.content_versions WHERE id=:id"
                ),
                {"id": old_id},
            ).scalar_one()
            with pytest.raises(DBAPIError), clearing.begin_nested():
                clearing.execute(
                    text(
                        "SELECT primary_signal.publish_reviewed("
                        ":story,:revision,:fingerprint,NULL,:actor,:reason)"
                    ),
                    {
                        "story": old_story,
                        "revision": old_revision,
                        "fingerprint": old_fingerprint,
                        "actor": decision.actor,
                        "reason": decision.reason,
                    },
                )
            clearing.rollback()
        # Make this fixture ineligible before exercising a separate race.
        with admin.begin() as connection:
            connection.execute(
                text(
                    "UPDATE primary_signal.articles SET current_content_version_id=:version "
                    "WHERE id=:article"
                ),
                {"version": old_id, "article": old_article},
            )
        # A concurrent same-hash observation owns the article lock first. The
        # cleaner skips it without waiting or taking a version lock; making
        # that version current resets its retention clock.
        with admin.begin() as holding:
            holding.execute(
                text("SELECT id FROM primary_signal.articles WHERE id=:id FOR UPDATE"),
                {"id": race_article},
            )
            assert run_batch(maintenance, limit=500) == ()
            holding.execute(
                text(
                    "UPDATE primary_signal.articles SET current_content_version_id=:version "
                    "WHERE id=:article"
                ),
                {"version": race_id, "article": race_article},
            )
        with admin.begin() as connection:
            publishing_article, publishing_id = _insert_candidate(connection, age_days=91)
        publishing_story, publishing_revision, publishing_fingerprint = _create_draft(
            writer, publishing_article, publishing_id
        )
        # A publication's version share lock makes cleanup skip that row. The
        # publish transaction can then commit, and the historical reference
        # still protects the text after the lock is released.
        with admin.begin() as holding:
            holding.execute(
                text("SELECT id FROM primary_signal.content_versions WHERE id=:id FOR SHARE"),
                {"id": publishing_id},
            )
            assert run_batch(maintenance, limit=500) == ()
            decider.publish_reviewed(
                story_id=publishing_story,
                revision_id=publishing_revision,
                input_fingerprint=publishing_fingerprint,
                expected_current_revision_id=None,
                decision=decision,
            )
        with maintenance.begin() as connection:
            assert clear_expired_extracted_text(connection, limit=500) == ()
        with admin.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT extracted_text IS NOT NULL FROM primary_signal.content_versions WHERE id=:id"
                ),
                {"id": old_id},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT extracted_text IS NOT NULL FROM primary_signal.content_versions WHERE id=:id"
                ),
                {"id": recent_id},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT extracted_text IS NOT NULL FROM primary_signal.content_versions WHERE id=:id"
                ),
                {"id": protected_id},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT extracted_text IS NOT NULL FROM primary_signal.content_versions WHERE id=:id"
                ),
                {"id": publishing_id},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT current_content_version_id = :old FROM primary_signal.articles "
                    "WHERE id=:article"
                ),
                {"old": old_id, "article": old_article},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT normalized_content_hash IS NOT NULL FROM primary_signal.content_versions WHERE id=:id"
                ),
                {"id": old_id},
            ).scalar_one()
    finally:
        admin.dispose()
        maintenance.dispose()
        writer_engine.dispose()
        decision_engine.dispose()
