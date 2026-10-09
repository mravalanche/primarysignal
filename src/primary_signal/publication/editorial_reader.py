"""Bounded private editorial metadata reads through a read-only login.

This reader does not expose article text and cannot make publication decisions.
The admin web runtime must use its own authenticated boundary before calling it.
"""

import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import RowMapping

from primary_signal.db.engine import assert_database_role
from primary_signal.publication.models import validate_public_identifier

_ROLE_CHECK = text(
    """
WITH RECURSIVE memberships(role_oid) AS (
    SELECT member_of.roleid FROM pg_catalog.pg_auth_members AS member_of
    WHERE member_of.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
    UNION
    SELECT member_of.roleid FROM pg_catalog.pg_auth_members AS member_of
    JOIN memberships ON member_of.member = memberships.role_oid
)
SELECT session_user = current_user
    AND COALESCE((SELECT NOT (role.rolsuper OR role.rolcreaterole OR role.rolcreatedb
                             OR role.rolbypassrls OR role.rolreplication)
                  FROM pg_catalog.pg_roles AS role WHERE role.rolname = current_user), false)
    AND pg_catalog.pg_has_role(current_user, 'primary_signal_cap_editorial_read', 'USAGE')
    AND NOT pg_catalog.has_column_privilege(
        current_user, 'primary_signal.content_versions', 'extracted_text', 'SELECT')
    AND NOT EXISTS (
        SELECT 1 FROM memberships
        JOIN pg_catalog.pg_roles AS inherited ON inherited.oid = memberships.role_oid
        WHERE inherited.rolname <> 'primary_signal_cap_editorial_read'
    ) AS is_restricted_editorial_reader
"""
)


@dataclass(frozen=True, slots=True)
class EditorialStoryRow:
    story_id: uuid.UUID
    slug: str
    current_revision_id: uuid.UUID | None
    suppressed: bool
    created_at: datetime
    candidate_revision_id: uuid.UUID | None
    candidate_number: int | None
    candidate_status: str | None
    candidate_headline: str | None


@dataclass(frozen=True, slots=True)
class EditorialStoryPage:
    items: tuple[EditorialStoryRow, ...]
    next_position: tuple[datetime, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class EditorialRevision:
    id: uuid.UUID
    number: int
    status: str
    headline: str
    synthesis: str
    why_it_matters: str
    primary_topic: str
    story_type: str
    first_reported_at: datetime
    latest_material_update_at: datetime
    published_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EditorialSourceLineage:
    source_id: str
    position: int
    title: str
    publisher: str
    public_url: str
    first_published_at: datetime | None
    is_primary: bool
    article_id: uuid.UUID | None
    content_version_id: uuid.UUID | None
    content_hash: str | None
    fetched_at: datetime | None
    configured_source_key: str | None
    configured_source_name: str | None
    configured_source_enabled: bool | None
    current_canonical_url: str | None
    public_url_belongs_to_article: bool


@dataclass(frozen=True, slots=True)
class EditorialEvent:
    id: uuid.UUID
    revision_id: uuid.UUID
    from_status: str | None
    to_status: str
    actor: str
    reason: str
    input_fingerprint: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class EditorialStoryDetail:
    story: EditorialStoryRow
    revisions: tuple[EditorialRevision, ...]
    revisions_have_more: bool
    current_revision: EditorialRevision | None
    candidate_sources: tuple[EditorialSourceLineage, ...]
    sources_have_more: bool
    current_sources: tuple[EditorialSourceLineage, ...]
    current_sources_have_more: bool
    events: tuple[EditorialEvent, ...]
    events_have_more: bool


class EditorialReader:
    """Read metadata under the dedicated editorial capability only."""

    def __init__(self, engine: Engine, *, expected_role: str) -> None:
        if not expected_role:
            raise ValueError("expected_role is required")
        self._engine = engine
        self._expected_role = expected_role

    @contextmanager
    def _snapshot(self) -> Generator[Connection]:
        with self._engine.connect() as connection:
            if connection.in_transaction():
                connection.rollback()
            with connection.begin():
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                assert_database_role(connection, self._expected_role)
                if connection.execute(_ROLE_CHECK).scalar_one() is not True:
                    raise RuntimeError("editorial reader role lacks restricted read privileges")
                yield connection

    @staticmethod
    def _story(row: RowMapping) -> EditorialStoryRow:
        return EditorialStoryRow(
            story_id=row["story_id"],
            slug=row["slug"],
            current_revision_id=row["current_revision_id"],
            suppressed=row["suppressed"],
            created_at=row["created_at"],
            candidate_revision_id=row["candidate_revision_id"],
            candidate_number=row["candidate_number"],
            candidate_status=row["candidate_status"],
            candidate_headline=row["candidate_headline"],
        )

    def list_stories(
        self,
        *,
        limit: int = 20,
        before: tuple[datetime, uuid.UUID] | None = None,
    ) -> EditorialStoryPage:
        """List all states by stable creation order, without a review-queue count."""

        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if before is not None and (before[0].tzinfo is None or before[0].utcoffset() is None):
            raise ValueError("pagination time must include a timezone")
        with self._snapshot() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT story.id AS story_id, story.slug, story.current_revision_id, "
                        "story.suppressed, story.created_at, "
                        "candidate.id AS candidate_revision_id, "
                        "candidate.revision_number AS candidate_number, "
                        "candidate.status AS candidate_status, "
                        "candidate.headline AS candidate_headline "
                        "FROM primary_signal.stories AS story "
                        "LEFT JOIN LATERAL (SELECT id, revision_number, status, headline "
                        "FROM primary_signal.story_revisions WHERE story_id = story.id "
                        "ORDER BY revision_number DESC LIMIT 1) AS candidate ON true "
                        "WHERE (CAST(:before_time AS timestamptz) IS NULL "
                        "OR (story.created_at, story.id) "
                        "< (CAST(:before_time AS timestamptz), CAST(:before_id AS uuid))) "
                        "ORDER BY story.created_at DESC, story.id DESC LIMIT :row_limit"
                    ),
                    {
                        "before_time": before[0] if before else None,
                        "before_id": before[1] if before else None,
                        "row_limit": limit + 1,
                    },
                )
                .mappings()
                .all()
            )
        visible = tuple(self._story(row) for row in rows[:limit])
        next_position = (
            (visible[-1].created_at, visible[-1].story_id) if len(rows) > limit else None
        )
        return EditorialStoryPage(visible, next_position)

    def get_story(self, slug: str) -> EditorialStoryDetail | None:
        """Inspect one candidate and its bounded private history and provenance."""

        validate_public_identifier(slug, name="story slug", slug=True)
        with self._snapshot() as connection:
            story_row = (
                connection.execute(
                    text(
                        "SELECT story.id AS story_id, story.slug, story.current_revision_id, "
                        "story.suppressed, story.created_at, "
                        "candidate.id AS candidate_revision_id, "
                        "candidate.revision_number AS candidate_number, "
                        "candidate.status AS candidate_status, "
                        "candidate.headline AS candidate_headline "
                        "FROM primary_signal.stories AS story "
                        "LEFT JOIN LATERAL (SELECT id, revision_number, status, headline "
                        "FROM primary_signal.story_revisions WHERE story_id = story.id "
                        "ORDER BY revision_number DESC LIMIT 1) AS candidate ON true "
                        "WHERE story.slug = :slug"
                    ),
                    {"slug": slug},
                )
                .mappings()
                .one_or_none()
            )
            if story_row is None:
                return None
            story = self._story(story_row)
            revision_rows = (
                connection.execute(
                    text(
                        "SELECT id, revision_number, status, headline, synthesis, "
                        "why_it_matters, primary_topic, story_type, first_reported_at, "
                        "latest_material_update_at, published_at, created_at "
                        "FROM primary_signal.story_revisions WHERE story_id = :story_id "
                        "ORDER BY revision_number DESC LIMIT 21"
                    ),
                    {"story_id": story.story_id},
                )
                .mappings()
                .all()
            )
            current_revision_row = (
                connection.execute(
                    text(
                        "SELECT id, revision_number, status, headline, synthesis, "
                        "why_it_matters, primary_topic, story_type, first_reported_at, "
                        "latest_material_update_at, published_at, created_at "
                        "FROM primary_signal.story_revisions WHERE id = :revision_id "
                        "AND story_id = :story_id"
                    ),
                    {"revision_id": story.current_revision_id, "story_id": story.story_id},
                )
                .mappings()
                .one_or_none()
                if story.current_revision_id is not None
                else None
            )
            source_rows: Sequence[RowMapping] = (
                connection.execute(
                    text(
                        "SELECT relation.source_id, relation.position, relation.title, "
                        "relation.publisher, relation.public_url, "
                        "relation.first_published_at, relation.is_primary, "
                        "relation.article_id, relation.content_version_id, "
                        "version.normalized_content_hash AS content_hash, "
                        "version.fetched_at, configured.source_key AS configured_source_key, "
                        "configured.name AS configured_source_name, "
                        "configured.enabled AS configured_source_enabled, "
                        "canonical.normalized_url AS current_canonical_url, "
                        "EXISTS (SELECT 1 FROM primary_signal.article_urls AS linked "
                        "WHERE linked.article_id = relation.article_id "
                        "AND (linked.original_url = relation.public_url "
                        "OR linked.normalized_url = relation.public_url)) "
                        "AS public_url_belongs_to_article "
                        "FROM primary_signal.revision_sources AS relation "
                        "LEFT JOIN primary_signal.content_versions AS version "
                        "ON version.id = relation.content_version_id "
                        "AND version.article_id = relation.article_id "
                        "LEFT JOIN primary_signal.articles AS article "
                        "ON article.id = relation.article_id "
                        "LEFT JOIN primary_signal.sources AS configured "
                        "ON configured.id = article.source_id "
                        "LEFT JOIN primary_signal.article_urls AS canonical "
                        "ON canonical.id = article.current_canonical_url_id "
                        "WHERE relation.revision_id = :revision_id "
                        "ORDER BY relation.position LIMIT 51"
                    ),
                    {"revision_id": story.candidate_revision_id},
                )
                .mappings()
                .all()
                if story.candidate_revision_id is not None
                else ()
            )
            current_source_rows: Sequence[RowMapping] = (
                connection.execute(
                    text(
                        "SELECT relation.source_id, relation.position, relation.title, "
                        "relation.publisher, relation.public_url, "
                        "relation.first_published_at, relation.is_primary, "
                        "relation.article_id, relation.content_version_id, "
                        "version.normalized_content_hash AS content_hash, "
                        "version.fetched_at, configured.source_key AS configured_source_key, "
                        "configured.name AS configured_source_name, "
                        "configured.enabled AS configured_source_enabled, "
                        "canonical.normalized_url AS current_canonical_url, "
                        "EXISTS (SELECT 1 FROM primary_signal.article_urls AS linked "
                        "WHERE linked.article_id = relation.article_id "
                        "AND (linked.original_url = relation.public_url "
                        "OR linked.normalized_url = relation.public_url)) "
                        "AS public_url_belongs_to_article "
                        "FROM primary_signal.revision_sources AS relation "
                        "LEFT JOIN primary_signal.content_versions AS version "
                        "ON version.id = relation.content_version_id "
                        "AND version.article_id = relation.article_id "
                        "LEFT JOIN primary_signal.articles AS article "
                        "ON article.id = relation.article_id "
                        "LEFT JOIN primary_signal.sources AS configured "
                        "ON configured.id = article.source_id "
                        "LEFT JOIN primary_signal.article_urls AS canonical "
                        "ON canonical.id = article.current_canonical_url_id "
                        "WHERE relation.revision_id = :revision_id "
                        "ORDER BY relation.position LIMIT 51"
                    ),
                    {"revision_id": story.current_revision_id},
                )
                .mappings()
                .all()
                if story.current_revision_id is not None
                and story.current_revision_id != story.candidate_revision_id
                else source_rows
            )
            event_rows = (
                connection.execute(
                    text(
                        "SELECT id, revision_id, from_status, to_status, actor, reason, "
                        "input_fingerprint, occurred_at "
                        "FROM primary_signal.publication_events WHERE story_id = :story_id "
                        "ORDER BY occurred_at DESC, id DESC LIMIT 51"
                    ),
                    {"story_id": story.story_id},
                )
                .mappings()
                .all()
            )

        def make_revision(row: RowMapping) -> EditorialRevision:
            return EditorialRevision(
                id=row["id"],
                number=row["revision_number"],
                status=row["status"],
                headline=row["headline"],
                synthesis=row["synthesis"],
                why_it_matters=row["why_it_matters"],
                primary_topic=row["primary_topic"],
                story_type=row["story_type"],
                first_reported_at=row["first_reported_at"],
                latest_material_update_at=row["latest_material_update_at"],
                published_at=row["published_at"],
                created_at=row["created_at"],
            )

        revisions = tuple(make_revision(row) for row in revision_rows[:20])
        current_revision = (
            make_revision(current_revision_row) if current_revision_row is not None else None
        )
        sources = tuple(EditorialSourceLineage(**row) for row in source_rows[:50])
        current_sources = tuple(EditorialSourceLineage(**row) for row in current_source_rows[:50])
        events = tuple(EditorialEvent(**row) for row in event_rows[:50])
        return EditorialStoryDetail(
            story=story,
            revisions=revisions,
            revisions_have_more=len(revision_rows) > 20,
            current_revision=current_revision,
            candidate_sources=sources,
            sources_have_more=len(source_rows) > 50,
            current_sources=current_sources,
            current_sources_have_more=len(current_source_rows) > 50,
            events=events,
            events_have_more=len(event_rows) > 50,
        )
