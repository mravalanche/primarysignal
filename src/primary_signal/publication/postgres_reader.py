"""PostgreSQL reader for the curated, read-only public projection."""

from collections import defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, bindparam, text
from sqlalchemy.engine import Connection, RowMapping

from primary_signal.publication.cursor import StoryCursor, decode_cursor, encode_cursor
from primary_signal.publication.models import (
    PublicSignal,
    PublicSignalKind,
    PublicSource,
    PublicStory,
    PublicStoryPage,
    PublicStorySummary,
    PublicTag,
    StoryListQuery,
    StoryType,
    TagKind,
    Topic,
    validate_public_identifier,
)

_STORY_COLUMNS = (
    "slug, headline, synthesis, why_it_matters, primary_topic, story_type, "
    "uk_relevant, first_reported_at, latest_material_update_at, source_count"
)


class PostgresStoryReader:
    """Read published stories with the restricted public database role."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        position = decode_cursor(query.cursor, query) if query.cursor is not None else None
        conditions: list[str] = []
        parameters: dict[str, Any] = {"page_size": query.limit + 1}
        if query.topic is not None:
            conditions.append("story.primary_topic = :topic")
            parameters["topic"] = query.topic.value
        if query.story_type is not None:
            conditions.append("story.story_type = :story_type")
            parameters["story_type"] = query.story_type.value
        if query.uk_relevant is not None:
            conditions.append("story.uk_relevant = :uk_relevant")
            parameters["uk_relevant"] = query.uk_relevant
        if query.tag_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM primary_signal_public.story_tags AS relation "
                "WHERE relation.slug = story.slug AND relation.tag_id = :tag_id)"
            )
            parameters["tag_id"] = query.tag_id
        if position is not None:
            conditions.append(
                "(story.latest_material_update_at < :cursor_time OR "
                "(story.latest_material_update_at = :cursor_time "
                "AND story.slug > :cursor_slug))"
            )
            parameters["cursor_time"] = position.latest_material_update_at
            parameters["cursor_slug"] = position.slug
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        statement = text(
            f"SELECT {_STORY_COLUMNS} FROM primary_signal_public.stories AS story"  # noqa: S608
            f"{where_clause} ORDER BY story.latest_material_update_at DESC, story.slug ASC "
            "LIMIT :page_size"
        )
        with self._snapshot() as connection:
            rows = connection.execute(statement, parameters).mappings().all()
            visible_rows = rows[: query.limit]
            children = self._load_children(connection, [row["slug"] for row in visible_rows])
            items = tuple(self._summary(row, children) for row in visible_rows)
        next_cursor = None
        if len(rows) > query.limit:
            last = items[-1]
            next_cursor = encode_cursor(
                StoryCursor(last.latest_material_update_at, last.slug), query
            )
        return PublicStoryPage(items=items, next_cursor=next_cursor)

    def get_story(self, slug: str) -> PublicStory | None:
        validate_public_identifier(slug, name="story slug", slug=True)
        with self._snapshot() as connection:
            row = (
                connection.execute(
                    text(
                        f"SELECT {_STORY_COLUMNS} FROM primary_signal_public.stories "  # noqa: S608
                        "WHERE slug = :slug"
                    ),
                    {"slug": slug},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            children = self._load_children(connection, [slug], include_sources=True)
            summary = self._summary(row, children)
            sources = children.sources[slug]
            return PublicStory(
                slug=summary.slug,
                headline=summary.headline,
                synthesis=summary.synthesis,
                why_it_matters=summary.why_it_matters,
                primary_topic=summary.primary_topic,
                story_type=summary.story_type,
                first_reported_at=summary.first_reported_at,
                latest_material_update_at=summary.latest_material_update_at,
                source_count=summary.source_count,
                tags=summary.tags,
                signals=summary.signals,
                uk_relevant=summary.uk_relevant,
                sources=tuple(sources),
            )

    def get_tag(self, tag_id: str) -> PublicTag | None:
        validate_public_identifier(tag_id, name="tag id")
        with self._snapshot() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, label, kind FROM primary_signal_public.tags WHERE id = :tag_id"
                    ),
                    {"tag_id": tag_id},
                )
                .mappings()
                .one_or_none()
            )
            return self._tag(row) if row is not None else None

    @contextmanager
    def _snapshot(self) -> Generator[Connection]:
        with self._engine.connect() as connection:
            # The engine role assertion may have autobegun a transaction.
            if connection.in_transaction():
                connection.rollback()
            with connection.begin():
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                yield connection

    @staticmethod
    def _tag(row: RowMapping) -> PublicTag:
        return PublicTag(id=row["id"], label=row["label"], kind=TagKind(row["kind"]))

    @staticmethod
    def _summary(row: RowMapping, children: _Children) -> PublicStorySummary:
        slug: str = row["slug"]
        signals = tuple(
            PublicSignal(kind=kind, evidence_source_ids=tuple(children.evidence[(slug, kind)]))
            for kind in children.signals[slug]
        )
        return PublicStorySummary(
            slug=slug,
            headline=row["headline"],
            synthesis=row["synthesis"],
            why_it_matters=row["why_it_matters"],
            primary_topic=Topic(row["primary_topic"]),
            story_type=StoryType(row["story_type"]),
            first_reported_at=row["first_reported_at"],
            latest_material_update_at=row["latest_material_update_at"],
            source_count=row["source_count"],
            tags=tuple(children.tags[slug]),
            signals=signals,
            uk_relevant=row["uk_relevant"],
        )

    def _load_children(
        self, connection: Connection, slugs: list[str], *, include_sources: bool = False
    ) -> _Children:
        children = _Children()
        if not slugs:
            return children
        parameters = {"slugs": slugs}

        def rows(sql: str) -> list[RowMapping]:
            statement = text(sql).bindparams(bindparam("slugs", expanding=True))
            return list(connection.execute(statement, parameters).mappings())

        for row in rows(
            "SELECT relation.slug, tag.id, tag.label, tag.kind "
            "FROM primary_signal_public.story_tags AS relation "
            "JOIN primary_signal_public.tags AS tag ON tag.id = relation.tag_id "
            "WHERE relation.slug IN :slugs ORDER BY relation.slug, relation.position"
        ):
            children.tags[row["slug"]].append(self._tag(row))
        for row in rows(
            "SELECT slug, kind FROM primary_signal_public.signals "
            "WHERE slug IN :slugs ORDER BY slug, kind"
        ):
            children.signals[row["slug"]].append(PublicSignalKind(row["kind"]))
        for row in rows(
            "SELECT slug, kind, source_id FROM primary_signal_public.signal_evidence "
            "WHERE slug IN :slugs ORDER BY slug, kind, source_id"
        ):
            children.evidence[(row["slug"], PublicSignalKind(row["kind"]))].append(row["source_id"])
        if include_sources:
            for row in rows(
                "SELECT slug, source_id, title, publisher, public_url, "
                "first_published_at, is_primary FROM primary_signal_public.sources "
                "WHERE slug IN :slugs ORDER BY slug, position"
            ):
                children.sources[row["slug"]].append(
                    PublicSource(
                        id=row["source_id"],
                        title=row["title"],
                        publisher=row["publisher"],
                        url=row["public_url"],
                        first_published_at=row["first_published_at"],
                        is_primary=row["is_primary"],
                    )
                )
        return children


class _Children:
    def __init__(self) -> None:
        self.tags: defaultdict[str, list[PublicTag]] = defaultdict(list)
        self.signals: defaultdict[str, list[PublicSignalKind]] = defaultdict(list)
        self.evidence: defaultdict[tuple[str, PublicSignalKind], list[str]] = defaultdict(list)
        self.sources: defaultdict[str, list[PublicSource]] = defaultdict(list)
