"""Disposable PostgreSQL proof of private read-only editorial metadata."""

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.publication.editorial_reader import (
    EditorialReader,
    assert_editorial_database_role,
)


@pytest.mark.postgres
def test_restricted_editorial_reader_sees_provenance_without_article_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_admin = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    editorial_url = os.environ.get("PRIMARY_SIGNAL_TEST_EDITORIAL_DATABASE_URL")
    public_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    if not all((admin_url, expected_admin, editorial_url, public_url)):
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires admin, editorial and public test DSNs")
        pytest.skip("configure the disposable PostgreSQL test database")
    assert admin_url is not None
    assert expected_admin is not None
    assert editorial_url is not None
    assert public_url is not None
    admin = create_engine(admin_url, hide_parameters=True)
    editorial = create_engine(editorial_url, hide_parameters=True)
    public = create_engine(public_url, hide_parameters=True)
    with admin.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_admin
    with editorial.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == "editorial_test"
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_admin)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    with editorial.connect() as connection:
        assert_editorial_database_role(connection, "editorial_test")
    with admin.connect() as connection, pytest.raises(UnexpectedDatabaseRoleError):
        assert_editorial_database_role(connection, "editorial_test")
    try:
        with admin.begin() as connection:
            connection.execute(
                text("GRANT UPDATE (suppressed) ON primary_signal.stories TO editorial_test")
            )
        with editorial.connect() as connection, pytest.raises(RuntimeError, match="restricted"):
            assert_editorial_database_role(connection, "editorial_test")
    finally:
        with admin.begin() as connection:
            connection.execute(
                text("REVOKE UPDATE (suppressed) ON primary_signal.stories FROM editorial_test")
            )
    try:
        with admin.begin() as connection:
            connection.execute(
                text("GRANT SELECT ON primary_signal.admin_sessions TO editorial_test")
            )
        with editorial.connect() as connection, pytest.raises(RuntimeError, match="restricted"):
            assert_editorial_database_role(connection, "editorial_test")
    finally:
        with admin.begin() as connection:
            connection.execute(
                text("REVOKE SELECT ON primary_signal.admin_sessions FROM editorial_test")
            )
    try:
        with admin.begin() as connection:
            connection.execute(
                text(
                    "GRANT SELECT (csrf_secret) ON primary_signal.admin_sessions TO editorial_test"
                )
            )
        with editorial.connect() as connection, pytest.raises(RuntimeError, match="restricted"):
            assert_editorial_database_role(connection, "editorial_test")
    finally:
        with admin.begin() as connection:
            connection.execute(
                text(
                    "REVOKE SELECT (csrf_secret) ON primary_signal.admin_sessions FROM editorial_test"
                )
            )

    source_id, article_id, article_url_id, attempt_id, version_id = (uuid.uuid7() for _ in range(5))
    story_id, revision_id, event_id = (uuid.uuid7() for _ in range(3))
    marker = uuid.uuid7().hex
    slug = f"editorial-reader-{marker}-{story_id.hex}"
    link = f"https://public.example/notice/{article_id.hex}"
    now = datetime.now(UTC)
    try:
        with admin.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO primary_signal.sources(id,source_key,name,homepage_url) "
                    "VALUES (:id,:key,'Synthetic publisher','https://public.example/')"
                ),
                {"id": source_id, "key": f"editorial-{source_id.hex}"},
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
                    "url": link,
                    "hash": hashlib.sha256(link.encode()).hexdigest(),
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.articles SET current_canonical_url_id=:url "
                    "WHERE id=:article"
                ),
                {"url": article_url_id, "article": article_id},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.fetch_attempts "
                    "(id,article_id,retrieval_strategy,requested_url,redirect_chain,status,"
                    "started_at) VALUES (:id,:article,'direct_http',:url,'[]'::jsonb,'running',:now)"
                ),
                {"id": attempt_id, "article": article_id, "url": link, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.content_versions "
                    "(id,article_id,origin_fetch_attempt_id,raw_response_hash,"
                    "normalized_content_hash,normalization_version,extracted_text,"
                    "extractor_name,extractor_version,fetched_at) "
                    "VALUES (:id,:article,:attempt,:raw,:normalized,1,'Private synthetic body',"
                    "'synthetic','1',:now)"
                ),
                {
                    "id": version_id,
                    "article": article_id,
                    "attempt": attempt_id,
                    "raw": "b" * 64,
                    "normalized": "c" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.fetch_attempts SET status='fetched',"
                    "completed_at=:now,resulting_content_version_id=:version WHERE id=:id"
                ),
                {"now": now, "version": version_id, "id": attempt_id},
            )
            connection.execute(
                text("INSERT INTO primary_signal.stories(id,slug) VALUES (:id,:slug)"),
                {"id": story_id, "slug": slug},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.story_revisions "
                    "(id,story_id,revision_number,headline,synthesis,why_it_matters,"
                    "primary_topic,story_type,first_reported_at,latest_material_update_at) "
                    "VALUES (:id,:story,1,'Synthetic headline','Synthetic summary',"
                    "'Synthetic relevance','security-engineering','advisory',:now,:now)"
                ),
                {"id": revision_id, "story": story_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_sources "
                    "(revision_id,source_id,position,title,publisher,public_url,is_primary,"
                    "article_id,content_version_id) "
                    "VALUES (:revision,'vendor',1,'Synthetic advisory','Synthetic publisher',"
                    ":url,true,:article,:version)"
                ),
                {
                    "revision": revision_id,
                    "url": link,
                    "article": article_id,
                    "version": version_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.publication_events "
                    "(id,story_id,revision_id,to_status,actor,reason) "
                    "VALUES (:id,:story,:revision,'draft','test-editor','Synthetic draft')"
                ),
                {"id": event_id, "story": story_id, "revision": revision_id},
            )

        reader = EditorialReader(editorial, expected_role="editorial_test")
        detail = reader.get_story(slug)
        assert detail is not None
        assert detail.story.candidate_revision_id == revision_id
        assert detail.story.current_revision_id is None
        assert detail.revisions[0].status == "draft"
        assert detail.events[0].actor == "test-editor"
        source = detail.candidate_sources[0]
        assert source.article_id == article_id
        assert source.content_version_id == version_id
        assert source.public_url_belongs_to_article
        assert source.current_canonical_url == link
        assert source.configured_source_enabled is True
        assert reader.list_stories(limit=1).items[0].slug == slug
        assert reader.list_stories(q=marker, state="draft").items[0].candidate_topic == (
            "security-engineering"
        )
        assert (
            reader.list_stories(q=marker, source=f"editorial-{source_id.hex}").items[0].slug == slug
        )
        assert reader.list_stories(q=marker, topic="research-and-tools").items == ()
        assert reader.list_stories(q="%", source=f"editorial-{source_id.hex}").items == ()
        assert reader.list_stories(q="_", source=f"editorial-{source_id.hex}").items == ()
        assert reader.list_stories(q="' OR 1=1 --", source=f"editorial-{source_id.hex}").items == ()
        with admin.begin() as connection:
            for offset, suppressed in ((1, False), (2, True)):
                extra_story, extra_revision = uuid.uuid7(), uuid.uuid7()
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.stories(id,slug,suppressed,created_at) "
                        "VALUES (:id,:slug,:suppressed,:created_at)"
                    ),
                    {
                        "id": extra_story,
                        "slug": f"editorial-reader-{marker}-{extra_story.hex}",
                        "suppressed": suppressed,
                        "created_at": now + timedelta(minutes=offset),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.story_revisions "
                        "(id,story_id,revision_number,headline,synthesis,why_it_matters,"
                        "primary_topic,story_type,first_reported_at,latest_material_update_at) "
                        "VALUES (:id,:story,1,'Additional synthetic story','Synthetic summary',"
                        "'Synthetic relevance','research-and-tools','research',:now,:now)"
                    ),
                    {"id": extra_revision, "story": extra_story, "now": now},
                )
        pages: list[str] = []
        before = None
        for _ in range(3):
            page = reader.list_stories(q=marker, limit=1, before=before)
            assert len(page.items) == 1
            pages.append(page.items[0].slug)
            before = page.next_position
        assert len(set(pages)) == 3
        assert before is None
        assert reader.list_stories(q=marker, state="suppressed").items[0].slug == pages[0]
        assert len(reader.list_stories(q=marker, state="draft").items) == 2
        assert len(reader.list_stories(q=marker, topic="research-and-tools").items) == 2
        assert len(reader.list_stories(q=marker, source=f"editorial-{source_id.hex}").items) == 1
        successor_id = uuid.uuid7()
        with admin.begin() as connection:
            connection.execute(
                text(
                    "UPDATE primary_signal.story_revisions SET status='validated' "
                    "WHERE id=:revision"
                ),
                {"revision": revision_id},
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.story_revisions SET status='published', "
                    "published_at=:now WHERE id=:revision"
                ),
                {"revision": revision_id, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.stories SET current_revision_id=:revision "
                    "WHERE id=:story"
                ),
                {"revision": revision_id, "story": story_id},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.story_revisions "
                    "(id,story_id,revision_number,headline,synthesis,why_it_matters,"
                    "primary_topic,story_type,first_reported_at,latest_material_update_at) "
                    "VALUES (:id,:story,2,'Synthetic successor','Updated synthetic summary',"
                    "'Synthetic relevance','security-engineering','advisory',:now,:now)"
                ),
                {"id": successor_id, "story": story_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_sources "
                    "(revision_id,source_id,position,title,publisher,public_url,is_primary,"
                    "article_id,content_version_id) "
                    "VALUES (:revision,'vendor',1,'Successor advisory','Synthetic publisher',"
                    ":url,true,:article,:version)"
                ),
                {
                    "revision": successor_id,
                    "url": link,
                    "article": article_id,
                    "version": version_id,
                },
            )
        comparison = reader.get_story(slug)
        assert comparison is not None
        assert comparison.story.candidate_revision_id == successor_id
        assert comparison.current_revision is not None
        assert comparison.current_revision.id == revision_id
        assert comparison.current_revision.headline == "Synthetic headline"
        assert comparison.current_sources[0].title == "Synthetic advisory"
        assert comparison.candidate_sources[0].title == "Successor advisory"
        with editorial.connect() as connection:
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege(current_user, "
                    "'primary_signal.content_versions', 'extracted_text', 'SELECT')"
                )
            ).scalar_one()
        try:
            with admin.begin() as connection:
                connection.execute(
                    text(
                        "GRANT SELECT (extracted_text) ON "
                        "primary_signal.content_versions TO editorial_test"
                    )
                )
            with pytest.raises(RuntimeError, match="editorial reader role"):
                reader.get_story(slug)
        finally:
            with admin.begin() as connection:
                connection.execute(
                    text(
                        "REVOKE SELECT (extracted_text) ON "
                        "primary_signal.content_versions FROM editorial_test"
                    )
                )
        with pytest.raises(DBAPIError), editorial.begin() as connection:
            connection.execute(text("UPDATE primary_signal.stories SET suppressed=true"))
        with pytest.raises(DBAPIError), public.begin() as connection:
            connection.execute(text("SELECT slug FROM primary_signal.stories"))
        with pytest.raises(RuntimeError, match="editorial reader role"):
            EditorialReader(public, expected_role="public_test").get_story(slug)
    finally:
        # Publication history is immutable; only unique synthetic data remains
        # in the disposable database that the test harness destroys.
        editorial.dispose()
        public.dispose()
        admin.dispose()
