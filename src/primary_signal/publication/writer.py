"""Draft creation and database-mediated publication decisions.

This trusted backend API does not authenticate an operator. A web caller must
bind the decision actor and expected public revision to a verified session.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.db.engine import assert_database_role
from primary_signal.publication.models import PublicSource, PublicStory


class PublicationConflict(ValueError):
    """The draft or current pointer changed before the decision completed."""


_WRITER_ROLE_CHECK = text(
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
    AND pg_catalog.pg_has_role(current_user, 'primary_signal_cap_publication_write', 'USAGE')
    AND pg_catalog.has_function_privilege(
        current_user, 'primary_signal.finalize_draft(uuid, uuid)', 'EXECUTE')
    AND pg_catalog.has_function_privilege(
        current_user, 'primary_signal.lock_story_for_draft(text)', 'EXECUTE')
    AND pg_catalog.has_function_privilege(
        current_user, 'primary_signal.publish_reviewed(uuid, uuid, text, uuid, text, text)',
        'EXECUTE')
    AND pg_catalog.has_function_privilege(
        current_user, 'primary_signal.suppress_reviewed(uuid, uuid, text, text)', 'EXECUTE')
    AND NOT pg_catalog.has_column_privilege(
        current_user, 'primary_signal.stories', 'current_revision_id', 'UPDATE')
    AND NOT pg_catalog.has_column_privilege(
        current_user, 'primary_signal.stories', 'suppressed', 'UPDATE')
    AND NOT pg_catalog.has_column_privilege(
        current_user, 'primary_signal.story_revisions', 'status', 'UPDATE')
    AND NOT pg_catalog.has_column_privilege(
        current_user, 'primary_signal.publication_events', 'id', 'INSERT')
    AND NOT EXISTS (
        SELECT 1 FROM memberships
        JOIN pg_catalog.pg_roles AS inherited ON inherited.oid = memberships.role_oid
        WHERE inherited.rolname <> 'primary_signal_cap_publication_write'
    ) AS is_restricted_publication_writer
"""
)


def assert_publication_writer_role(connection: Connection) -> None:
    """Reject a writer login with missing or additional inherited capabilities."""
    if connection.execute(_WRITER_ROLE_CHECK).scalar_one() is not True:
        raise RuntimeError("publication writer role lacks the required restricted privileges")


@dataclass(frozen=True, slots=True)
class DraftReference:
    """One visible source tied to an immutable fetched content version."""

    source: PublicSource
    article_id: uuid.UUID
    content_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OperatorDecision:
    actor: str
    reason: str

    def __post_init__(self) -> None:
        if self.actor == "system" or not self.actor.strip() or len(self.actor) > 160:
            raise ValueError("a named operator is required")
        if not self.reason.strip() or len(self.reason) > 1024:
            raise ValueError("a bounded decision reason is required")


class PublicationWriter:
    """Write through a dedicated login with the publication capability only."""

    def __init__(self, engine: Engine, *, expected_role: str) -> None:
        if not expected_role:
            raise ValueError("expected_role is required")
        self.engine = engine
        self.expected_role = expected_role

    def _check_connection(self, connection: Connection) -> None:
        assert_database_role(connection, self.expected_role)
        assert_publication_writer_role(connection)

    def create_draft(
        self, story: PublicStory, references: tuple[DraftReference, ...]
    ) -> tuple[uuid.UUID, uuid.UUID, str]:
        """Create a successor without changing the current public revision."""
        if (
            len(references) != len(story.sources)
            or tuple(ref.source for ref in references) != story.sources
        ):
            raise ValueError("references must match the public source trail in order")
        if story.tags or story.signals:
            raise ValueError("this writer slice does not yet support tags or signals")
        if not references:
            raise ValueError("a draft requires a source")
        revision_id = uuid.uuid7()
        with self.engine.begin() as connection:
            self._check_connection(connection)
            row = (
                connection.execute(
                    text("SELECT id, suppressed FROM primary_signal.lock_story_for_draft(:slug)"),
                    {"slug": story.slug},
                )
                .mappings()
                .first()
            )
            if row is None:
                story_id = uuid.uuid7()
                connection.execute(
                    text("INSERT INTO primary_signal.stories(id,slug) VALUES (:id,:slug)"),
                    {"id": story_id, "slug": story.slug},
                )
            else:
                if row["suppressed"]:
                    raise PublicationConflict("story is suppressed")
                story_id = row["id"]
            number = connection.execute(
                text(
                    "SELECT COALESCE(MAX(revision_number),0)+1 FROM "
                    "primary_signal.story_revisions WHERE story_id=:story_id"
                ),
                {"story_id": story_id},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO primary_signal.story_revisions "
                    "(id,story_id,revision_number,headline,synthesis,why_it_matters,"
                    "primary_topic,story_type,uk_relevant,first_reported_at,"
                    "latest_material_update_at) VALUES "
                    "(:id,:story_id,:number,:headline,:synthesis,:importance,:topic,:type,"
                    ":uk,:first,:latest)"
                ),
                {
                    "id": revision_id,
                    "story_id": story_id,
                    "number": number,
                    "headline": story.headline,
                    "synthesis": story.synthesis,
                    "importance": story.why_it_matters,
                    "topic": story.primary_topic.value,
                    "type": story.story_type.value,
                    "uk": story.uk_relevant,
                    "first": story.first_reported_at,
                    "latest": story.latest_material_update_at,
                },
            )
            for position, ref in enumerate(references, 1):
                source = ref.source
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.revision_sources "
                        "(revision_id,source_id,position,title,publisher,public_url,"
                        "first_published_at,is_primary,article_id,content_version_id) "
                        "VALUES (:revision,:source_id,:position,:title,:publisher,:url,"
                        ":published,:primary,:article,:version)"
                    ),
                    {
                        "revision": revision_id,
                        "source_id": source.id,
                        "position": position,
                        "title": source.title,
                        "publisher": source.publisher,
                        "url": source.url,
                        "published": source.first_published_at,
                        "primary": source.is_primary,
                        "article": ref.article_id,
                        "version": ref.content_version_id,
                    },
                )
            fingerprint = connection.execute(
                text("SELECT primary_signal.finalize_draft(:story, :revision)"),
                {"story": story_id, "revision": revision_id},
            ).scalar_one()
        return story_id, revision_id, fingerprint

    def publish_reviewed(
        self,
        *,
        story_id: uuid.UUID,
        revision_id: uuid.UUID,
        input_fingerprint: str,
        expected_current_revision_id: uuid.UUID | None,
        decision: OperatorDecision,
    ) -> None:
        """Publish a reviewed draft if its snapshot and current pointer agree."""
        if len(input_fingerprint) != 64 or any(
            c not in "0123456789abcdef" for c in input_fingerprint
        ):
            raise ValueError("input fingerprint is invalid")
        with self.engine.begin() as connection:
            self._check_connection(connection)
            try:
                connection.execute(
                    text(
                        "SELECT primary_signal.publish_reviewed(:story, :revision, :fingerprint, "
                        "CAST(:expected_current AS uuid), :actor, :reason)"
                    ),
                    {
                        "story": story_id,
                        "revision": revision_id,
                        "fingerprint": input_fingerprint,
                        "expected_current": expected_current_revision_id,
                        "actor": decision.actor,
                        "reason": decision.reason,
                    },
                )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "P0001":
                    raise PublicationConflict(
                        "publication decision conflicts with stored state"
                    ) from exc
                raise

    def suppress(
        self,
        *,
        story_id: uuid.UUID,
        expected_current_revision_id: uuid.UUID,
        decision: OperatorDecision,
    ) -> None:
        """Hide a current story and retain the decision in append-only history."""
        with self.engine.begin() as connection:
            self._check_connection(connection)
            try:
                connection.execute(
                    text(
                        "SELECT primary_signal.suppress_reviewed(:story, :expected_current, "
                        ":actor, :reason)"
                    ),
                    {
                        "story": story_id,
                        "expected_current": expected_current_revision_id,
                        "actor": decision.actor,
                        "reason": decision.reason,
                    },
                )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "P0001":
                    raise PublicationConflict(
                        "publication decision conflicts with stored state"
                    ) from exc
                raise
