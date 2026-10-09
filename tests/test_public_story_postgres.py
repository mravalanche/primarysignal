"""PostgreSQL public story reader behavior and restricted-role access."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from primary_signal.entrypoints.web import assert_public_database_role
from primary_signal.publication import (
    InvalidCursor,
    PostgresStoryReader,
    StoryListQuery,
    TagKind,
    Topic,
)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows)

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one_or_none(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


UPDATED = datetime(2026, 1, 2, tzinfo=UTC)
STORIES = [
    {
        "slug": slug,
        "headline": f"Synthetic {slug}",
        "synthesis": "Synthetic synthesis",
        "why_it_matters": "Synthetic relevance",
        "primary_topic": "security-engineering",
        "story_type": "advisory",
        "uk_relevant": False,
        "first_reported_at": datetime(2026, 1, 1, tzinfo=UTC),
        "latest_material_update_at": UPDATED,
        "source_count": 1,
    }
    for slug in ("synthetic-a", "synthetic-b")
]


def _reader() -> tuple[PostgresStoryReader, list[tuple[str, dict[str, Any]]]]:
    engine = MagicMock(spec=Engine)
    connection = engine.connect.return_value.__enter__.return_value
    statements: list[tuple[str, dict[str, Any]]] = []

    def execute(statement: object, parameters: dict[str, Any] | None = None) -> _Rows:
        sql = str(statement)
        values = parameters or {}
        statements.append((sql, values))
        if sql.startswith("SET TRANSACTION"):
            return _Rows([])
        if "FROM primary_signal_public.stories" in sql:
            if "WHERE slug = :slug" in sql:
                return _Rows([row for row in STORIES if row["slug"] == values["slug"]])
            if "cursor_slug" in values:
                return _Rows([STORIES[1]])
            return _Rows(STORIES)
        if "FROM primary_signal_public.story_tags" in sql:
            return _Rows(
                [
                    {
                        "slug": slug,
                        "id": "synthetic-tag",
                        "label": "Synthetic tag",
                        "kind": "curated",
                    }
                    for slug in values["slugs"]
                ]
            )
        if "FROM primary_signal_public.signals" in sql:
            return _Rows([{"slug": slug, "kind": "official-advisory"} for slug in values["slugs"]])
        if "FROM primary_signal_public.signal_evidence" in sql:
            return _Rows(
                [
                    {"slug": slug, "kind": "official-advisory", "source_id": "synthetic-source"}
                    for slug in values["slugs"]
                ]
            )
        if "FROM primary_signal_public.sources" in sql:
            return _Rows(
                [
                    {
                        "slug": slug,
                        "source_id": "synthetic-source",
                        "title": "Synthetic source",
                        "publisher": "Public Example",
                        "public_url": "https://public.example/story",
                        "first_published_at": None,
                        "is_primary": True,
                    }
                    for slug in values["slugs"]
                ]
            )
        if "FROM primary_signal_public.tags" in sql:
            return _Rows(
                [{"id": "synthetic-tag", "label": "Synthetic tag", "kind": "curated"}]
                if values["tag_id"] == "synthetic-tag"
                else []
            )
        raise AssertionError(sql)

    connection.execute.side_effect = execute
    return PostgresStoryReader(cast(Engine, engine)), statements


def test_bad_cursor_is_rejected_before_database_access() -> None:
    engine = MagicMock(spec=Engine)
    reader = PostgresStoryReader(cast(Engine, engine))
    with pytest.raises(InvalidCursor):
        reader.list_stories(StoryListQuery(limit=10, cursor="not-a-cursor"))
    engine.connect.assert_not_called()


def test_keyset_page_and_hydrated_public_children() -> None:
    reader, statements = _reader()
    query = StoryListQuery(limit=1, topic=Topic.SECURITY_ENGINEERING, tag_id="synthetic-tag")
    first = reader.list_stories(query)
    assert [item.slug for item in first.items] == ["synthetic-a"]
    assert first.next_cursor is not None
    assert first.items[0].tags[0].id == "synthetic-tag"
    assert first.items[0].signals[0].evidence_source_ids == ("synthetic-source",)
    sql, values = statements[1]
    assert "primary_signal_public.stories" in sql
    assert "primary_signal_public.story_tags" in sql
    assert "LIMIT :page_size" in sql
    assert values["page_size"] == 2
    assert values["tag_id"] == "synthetic-tag"
    assert statements[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

    second = reader.list_stories(
        StoryListQuery(
            limit=1,
            cursor=first.next_cursor,
            topic=Topic.SECURITY_ENGINEERING,
            tag_id="synthetic-tag",
        )
    )
    assert [item.slug for item in second.items] == ["synthetic-b"]
    assert second.next_cursor is None
    assert any("story.slug > :cursor_slug" in sql for sql, _ in statements)
    with pytest.raises(InvalidCursor):
        reader.list_stories(StoryListQuery(limit=1, cursor=first.next_cursor))

    story = reader.get_story("synthetic-a")
    assert story is not None
    assert [source.id for source in story.sources] == ["synthetic-source"]
    assert story.signals[0].evidence_source_ids == ("synthetic-source",)
    tag = reader.get_tag("synthetic-tag")
    assert tag is not None and tag.kind is TagKind.CURATED
    assert reader.get_story("synthetic-missing") is None


@pytest.mark.postgres
def test_restricted_public_login_can_read_projection() -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    if not url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("restricted public login DSN is required")
        pytest.skip("set PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            assert connection.execute(text("SELECT current_user")).scalar_one() == "public_test"
        reader = PostgresStoryReader(engine)
        page = reader.list_stories(StoryListQuery(limit=2))
        for summary in page.items:
            story = reader.get_story(summary.slug)
            assert story is not None and story.source_count == len(story.sources)
            assert reader.get_story(summary.slug) is not None
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_public_web_rejects_privileged_login() -> None:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    public_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    if not admin_url or not public_url:
        pytest.skip("configure disposable PostgreSQL test database")
    admin = create_engine(admin_url, hide_parameters=True)
    public = create_engine(public_url, hide_parameters=True)
    try:
        with admin.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            with pytest.raises(RuntimeError, match="restricted privileges"):
                assert_public_database_role(connection)
            connection.execute(text("SET LOCAL ROLE public_test"))
            with pytest.raises(RuntimeError, match="restricted privileges"):
                assert_public_database_role(connection)
        with public.connect() as connection:
            assert_public_database_role(connection)
    finally:
        admin.dispose()
        public.dispose()


@pytest.mark.postgres
def test_public_web_rejects_replication_login() -> None:
    if os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true":
        pytest.skip("requires the disposable PostgreSQL test cluster")
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("configure disposable PostgreSQL test database")
    admin = create_engine(admin_url, hide_parameters=True)
    role_name = "synthetic_public_replication_test"
    try:
        with admin.begin() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            connection.execute(
                text("CREATE ROLE synthetic_public_replication_test LOGIN REPLICATION")
            )
            connection.execute(
                text("GRANT primary_signal_cap_public_read TO synthetic_public_replication_test")
            )
        replication_url = make_url(admin_url).set(username=role_name)
        replication = create_engine(replication_url, hide_parameters=True)
        try:
            with (
                replication.connect() as connection,
                pytest.raises(RuntimeError, match="restricted privileges"),
            ):
                assert_public_database_role(connection)
        finally:
            replication.dispose()
    finally:
        admin.dispose()


@pytest.mark.postgres
def test_synthetic_projection_order_and_children_with_restricted_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use committed synthetic rows only in a declared disposable test database."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    if os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true":
        pytest.skip("synthetic committed fixture requires a disposable test database")
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    public_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not admin_url or not public_url or not expected_role:
        pytest.fail("disposable database flag requires admin and restricted public test URLs")
    admin = create_engine(admin_url, hide_parameters=True)
    public = create_engine(public_url, hide_parameters=True)
    try:
        with admin.connect() as check:
            database_name = str(check.execute(text("SELECT current_database()")).scalar_one())
            assert database_name.endswith("_test")
            assert check.execute(text("SELECT current_user")).scalar_one() == expected_role
        with public.connect() as check:
            assert check.execute(text("SELECT current_database()")).scalar_one() == database_name
            assert check.execute(text("SELECT current_user")).scalar_one() == "public_test"
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
        command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

        with admin.begin() as connection:
            nonce = uuid.uuid4().hex
            slugs = [f"synthetic-{nonce}-{letter}" for letter in "abcd"]
            tag_id = f"synthetic-{nonce}-tag"
            connection.execute(
                text(
                    "INSERT INTO primary_signal.tags(id,label,kind) "
                    "VALUES (:id,'Synthetic tag','curated')"
                ),
                {"id": tag_id},
            )
            for index, slug in enumerate(slugs):
                story_id = uuid.uuid4()
                revision_id = uuid.uuid4()
                source_id = f"synthetic-{nonce}-source-{index}"
                connection.execute(
                    text("INSERT INTO primary_signal.stories(id,slug) VALUES (:id,:slug)"),
                    {"id": story_id, "slug": slug},
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.story_revisions "
                        "(id,story_id,revision_number,headline,synthesis,why_it_matters,"
                        "primary_topic,story_type,first_reported_at,latest_material_update_at) "
                        "VALUES (:id,:story_id,1,'Synthetic headline','Synthetic synthesis',"
                        "'Synthetic relevance','security-engineering','advisory',"
                        "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00')"
                    ),
                    {"id": revision_id, "story_id": story_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.revision_sources "
                        "(revision_id,source_id,position,title,publisher,public_url) "
                        "VALUES (:revision,:source,1,'Synthetic source','Public Example',:url)"
                    ),
                    {
                        "revision": revision_id,
                        "source": source_id,
                        "url": f"https://public.example/{nonce}/{index}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.revision_tags(revision_id,tag_id,position) "
                        "VALUES (:revision,:tag_id,1)"
                    ),
                    {"revision": revision_id, "tag_id": tag_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.revision_signals(revision_id,kind) "
                        "VALUES (:revision,'official-advisory')"
                    ),
                    {"revision": revision_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.signal_evidence(revision_id,kind,source_id) "
                        "VALUES (:revision,'official-advisory',:source)"
                    ),
                    {"revision": revision_id, "source": source_id},
                )
                if index == 2:  # Draft must remain invisible.
                    continue
                connection.execute(
                    text(
                        "UPDATE primary_signal.story_revisions SET status='validated' "
                        "WHERE id=:revision"
                    ),
                    {"revision": revision_id},
                )
                connection.execute(
                    text(
                        "UPDATE primary_signal.story_revisions "
                        "SET status='published',published_at=now() WHERE id=:revision"
                    ),
                    {"revision": revision_id},
                )
                connection.execute(
                    text(
                        "UPDATE primary_signal.stories SET current_revision_id=:revision,"
                        "suppressed=:suppressed WHERE id=:story"
                    ),
                    {
                        "revision": revision_id,
                        "suppressed": index == 3,
                        "story": story_id,
                    },
                )

        reader = PostgresStoryReader(public)
        query = StoryListQuery(limit=1, tag_id=tag_id)
        first = reader.list_stories(query)
        assert [item.slug for item in first.items] == [slugs[0]]
        assert first.next_cursor is not None
        second = reader.list_stories(
            StoryListQuery(limit=1, tag_id=tag_id, cursor=first.next_cursor)
        )
        assert [item.slug for item in second.items] == [slugs[1]]
        assert second.next_cursor is None
        assert reader.get_story(slugs[2]) is None
        assert reader.get_story(slugs[3]) is None
        tag = reader.get_tag(tag_id)
        assert tag is not None and tag.kind is TagKind.CURATED
        story = reader.get_story(slugs[0])
        assert story is not None
        assert story.tags[0].id == tag_id
        assert story.sources[0].id == f"synthetic-{nonce}-source-0"
        assert story.signals[0].evidence_source_ids == (story.sources[0].id,)
    finally:
        public.dispose()
        admin.dispose()
