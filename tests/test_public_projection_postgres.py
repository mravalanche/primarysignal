"""The public database role can see only current published snapshots."""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.publication import PostgresStoryReader, StoryListQuery


@pytest.fixture
def migrated_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip("configure the disposable PostgreSQL test database")
    engine = create_engine(url)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    yield engine
    engine.dispose()


def _revision(
    connection: Connection, *, slug: str, number: int, status: str = "draft"
) -> tuple[uuid.UUID, uuid.UUID]:
    story_id = uuid.uuid7()
    revision_id = uuid.uuid7()
    connection.execute(
        text("INSERT INTO primary_signal.stories(id,slug) VALUES (:id,:slug)"),
        {"id": story_id, "slug": slug},
    )
    connection.execute(
        text(
            "INSERT INTO primary_signal.story_revisions "
            "(id,story_id,revision_number,status,headline,synthesis,why_it_matters,"
            "primary_topic,story_type,first_reported_at,latest_material_update_at) "
            "VALUES (:id,:story_id,:number,:status,'Synthetic headline','Synthetic summary',"
            "'Synthetic relevance','security-engineering','advisory',now(),now())"
        ),
        {"id": revision_id, "story_id": story_id, "number": number, "status": status},
    )
    return story_id, revision_id


def _publish(connection: Connection, story_id: uuid.UUID, revision_id: uuid.UUID) -> None:
    connection.execute(
        text("UPDATE primary_signal.story_revisions SET status='validated' WHERE id=:id"),
        {"id": revision_id},
    )
    connection.execute(
        text(
            "UPDATE primary_signal.story_revisions "
            "SET status='published',published_at=now() WHERE id=:id"
        ),
        {"id": revision_id},
    )
    connection.execute(
        text("UPDATE primary_signal.stories SET current_revision_id=:revision WHERE id=:id"),
        {"revision": revision_id, "id": story_id},
    )


@pytest.mark.postgres
def test_search_sees_published_match_but_not_draft_or_suppressed(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"needle{uuid.uuid4().hex}"
    slugs = [
        f"synthetic-search-{kind}-{uuid.uuid4().hex}"
        for kind in ("published", "draft", "suppressed")
    ]
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            revisions = [_revision(connection, slug=slug, number=1) for slug in slugs]
            for index, (_, revision_id) in enumerate(revisions):
                connection.execute(
                    text(
                        "UPDATE primary_signal.story_revisions SET headline=:headline "
                        "WHERE id=:revision_id"
                    ),
                    {"headline": f"Synthetic {marker} {index}", "revision_id": revision_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.revision_sources "
                        "(revision_id,source_id,position,title,publisher,public_url) "
                        "VALUES (:revision_id,'synthetic-source',1,'Synthetic source',"
                        "'Public Example','https://public.example/source')"
                    ),
                    {"revision_id": revision_id},
                )
            _publish(connection, *revisions[0])
            _publish(connection, *revisions[2])
            connection.execute(
                text("UPDATE primary_signal.stories SET suppressed=true WHERE id=:id"),
                {"id": revisions[2][0]},
            )
            connection.execute(text("SET LOCAL ROLE public_test"))
            assert connection.execute(text("SELECT current_user")).scalar_one() == "public_test"

            @contextmanager
            def same_transaction() -> Iterator[Connection]:
                yield connection

            reader = PostgresStoryReader(migrated_engine)
            monkeypatch.setattr(reader, "_snapshot", same_transaction)
            page = reader.list_stories(StoryListQuery(limit=10, q=marker))
            assert [item.slug for item in page.items] == [slugs[0]]
            assert page.items[0].headline == f"Synthetic {marker} 0"
            absent = f"absent{uuid.uuid4().hex}"
            assert reader.list_stories(StoryListQuery(limit=10, q=absent)).items == ()
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_projection_tracks_current_publication_and_children(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            story_id, first = _revision(connection, slug="synthetic-first", number=1)
            _draft_id, draft = _revision(connection, slug="synthetic-draft", number=1)
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_sources "
                    "(revision_id,source_id,position,title,publisher,public_url) VALUES "
                    "(:first,'source-one',1,'Source one','Public Example',"
                    "'https://public.example/source-one'),"
                    "(:draft,'source-draft',1,'Draft source','Public Example',"
                    "'https://public.example/source-draft')"
                ),
                {"first": first, "draft": draft},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.tags(id,label,kind) VALUES "
                    "('published-tag','Published tag','curated'),"
                    "('draft-tag','Draft tag','curated')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_tags(revision_id,tag_id,position) "
                    "VALUES (:first,'published-tag',1),(:draft,'draft-tag',1)"
                ),
                {"first": first, "draft": draft},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_signals(revision_id,kind) "
                    "VALUES (:first,'official-advisory'),(:draft,'actionable')"
                ),
                {"first": first, "draft": draft},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.signal_evidence(revision_id,kind,source_id) "
                    "VALUES (:first,'official-advisory','source-one'),"
                    "(:draft,'actionable','source-draft')"
                ),
                {"first": first, "draft": draft},
            )
            _publish(connection, story_id, first)
            connection.execute(text("SET LOCAL ROLE public_test"))
            for view in ("stories", "sources", "story_tags", "signals", "signal_evidence"):
                slugs = (
                    connection.execute(
                        text(f"SELECT DISTINCT slug FROM primary_signal_public.{view}")  # noqa: S608
                    )
                    .scalars()
                    .all()
                )
                assert slugs == ["synthetic-first"]
            assert connection.execute(
                text("SELECT id FROM primary_signal_public.tags")
            ).scalars().all() == ["published-tag"]
            assert (
                connection.execute(
                    text("SELECT source_count FROM primary_signal_public.stories")
                ).scalar_one()
                == 1
            )
            public_columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='primary_signal_public' AND table_name='sources'"
                    )
                )
            }
            assert {"article_id", "content_version_id"} & public_columns == set()
            connection.execute(text("RESET ROLE"))

            # A draft can coexist without changing the visible revision.
            second = uuid.uuid7()
            connection.execute(
                text(
                    "INSERT INTO primary_signal.story_revisions "
                    "(id,story_id,revision_number,headline,synthesis,why_it_matters,"
                    "primary_topic,story_type,first_reported_at,latest_material_update_at) "
                    "VALUES (:id,:story_id,2,'New draft','Draft summary','Draft relevance',"
                    "'security-engineering','advisory',now(),now())"
                ),
                {"id": second, "story_id": story_id},
            )
            connection.execute(text("SET LOCAL ROLE public_test"))
            assert (
                connection.execute(
                    text("SELECT revision_id FROM primary_signal_public.stories")
                ).scalar_one()
                == first
            )
            connection.execute(text("RESET ROLE"))

            # Suppression hides the story and every child view.
            connection.execute(
                text("UPDATE primary_signal.stories SET suppressed=true WHERE id=:id"),
                {"id": story_id},
            )
            connection.execute(text("SET LOCAL ROLE public_test"))
            for view in ("stories", "sources", "story_tags", "tags", "signals", "signal_evidence"):
                assert (
                    connection.execute(
                        text(f"SELECT count(*) FROM primary_signal_public.{view}")  # noqa: S608
                    ).scalar_one()
                    == 0
                )
            connection.execute(text("RESET ROLE"))
            connection.execute(
                text("UPDATE primary_signal.stories SET suppressed=false WHERE id=:id"),
                {"id": story_id},
            )
            connection.execute(
                text("UPDATE primary_signal.story_revisions SET status='superseded' WHERE id=:id"),
                {"id": first},
            )
            connection.execute(text("SET LOCAL ROLE public_test"))
            for view in ("stories", "sources", "story_tags", "tags", "signals", "signal_evidence"):
                assert (
                    connection.execute(
                        text(f"SELECT count(*) FROM primary_signal_public.{view}")  # noqa: S608
                    ).scalar_one()
                    == 0
                )
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_projection_role_cannot_read_base_or_mutate(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            assert connection.execute(
                text("SELECT has_schema_privilege('public_test','primary_signal_public','USAGE')")
            ).scalar_one()
            assert not connection.execute(
                text("SELECT has_schema_privilege('public_test','primary_signal_public','CREATE')")
            ).scalar_one()
            assert not connection.execute(
                text("SELECT has_schema_privilege('public_test','public','CREATE')")
            ).scalar_one()
            assert not connection.execute(
                text("SELECT pg_has_role('public_test','primary_signal_public_owner','MEMBER')")
            ).scalar_one()
            tables = connection.execute(
                text(
                    "SELECT table_name,column_name FROM information_schema.columns "
                    "WHERE table_schema='primary_signal'"
                )
            ).all()
            assert tables
            for table_name, column_name in tables:
                assert not connection.execute(
                    text("SELECT has_column_privilege('public_test',:table,:column,'SELECT')"),
                    {"table": f"primary_signal.{table_name}", "column": column_name},
                ).scalar_one()
            connection.execute(text("SET LOCAL ROLE public_test"))
            for query in (
                "SELECT extracted_text FROM primary_signal.content_versions LIMIT 1",
                "SELECT * FROM primary_signal.story_revisions LIMIT 1",
                "INSERT INTO primary_signal_public.tags(id,label,kind) VALUES ('x','x','curated')",
                "CREATE TABLE primary_signal_public.not_allowed(id integer)",
            ):
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text(query))
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_public_login_cannot_assume_owner(migrated_engine: Engine) -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    if not url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("restricted public login DSN is required")
        pytest.skip("set PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    public_engine = create_engine(url, hide_parameters=True)
    try:
        with public_engine.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == "public_test"
            assert (
                connection.execute(
                    text("SELECT count(*) FROM primary_signal_public.stories")
                ).scalar_one()
                >= 0
            )
            for query in (
                "SET ROLE primary_signal_public_owner",
                "SELECT extracted_text FROM primary_signal.content_versions LIMIT 1",
                "CREATE TABLE primary_signal_public.not_allowed(id integer)",
            ):
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text(query))
    finally:
        public_engine.dispose()


@pytest.mark.postgres
def test_revision_publication_guards(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            story_id, revision_id = _revision(connection, slug="synthetic-guard", number=1)
            other_story_id, other_revision = _revision(connection, slug="synthetic-other", number=1)
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.story_revisions "
                        "(id,story_id,revision_number,status,headline,synthesis,why_it_matters,"
                        "primary_topic,story_type,first_reported_at,latest_material_update_at,"
                        "published_at) VALUES (:id,:story_id,2,'published','A','B','C',"
                        "'security-engineering','news',now(),now(),now())"
                    ),
                    {"id": uuid.uuid7(), "story_id": story_id},
                )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_sources "
                    "(revision_id,source_id,position,title,publisher,public_url) "
                    "VALUES (:id,'source-one',1,'Source','Public Example',"
                    "'https://public.example/source')"
                ),
                {"id": revision_id},
            )
            connection.execute(
                text("UPDATE primary_signal.story_revisions SET status='validated' WHERE id=:id"),
                {"id": revision_id},
            )
            connection.execute(
                text("UPDATE primary_signal.story_revisions SET status='validated' WHERE id=:id"),
                {"id": other_revision},
            )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text(
                        "UPDATE primary_signal.story_revisions SET status='published',"
                        "published_at=now() WHERE id=:id"
                    ),
                    {"id": other_revision},
                )
            connection.execute(
                text(
                    "UPDATE primary_signal.story_revisions "
                    "SET status='published',published_at=now() WHERE id=:id"
                ),
                {"id": revision_id},
            )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("UPDATE primary_signal.stories SET current_revision_id=:rev WHERE id=:id"),
                    {"rev": revision_id, "id": other_story_id},
                )

            _signal_story_id, signal_revision = _revision(
                connection, slug="synthetic-signal", number=1
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_sources "
                    "(revision_id,source_id,position,title,publisher,public_url) "
                    "VALUES (:id,'signal-source',1,'Source','Public Example',"
                    "'https://public.example/signal')"
                ),
                {"id": signal_revision},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.revision_signals(revision_id,kind) "
                    "VALUES (:id,'official-advisory')"
                ),
                {"id": signal_revision},
            )
            connection.execute(
                text("UPDATE primary_signal.story_revisions SET status='validated' WHERE id=:id"),
                {"id": signal_revision},
            )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text(
                        "UPDATE primary_signal.story_revisions SET status='published',"
                        "published_at=now() WHERE id=:id"
                    ),
                    {"id": signal_revision},
                )
        finally:
            transaction.rollback()
