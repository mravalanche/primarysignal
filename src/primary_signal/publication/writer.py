"""Explicit, operator-reviewed publication transactions.

This is a narrow manual path. Automated publication must supply its own verified
contract gates before it may call a separate service; a caller assertion is not
an automatic publication gate.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, text

from primary_signal.db.engine import assert_database_role
from primary_signal.publication.models import PublicSource, PublicStory


class PublicationConflict(ValueError):
    """The draft or current pointer changed before the decision completed."""


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


def fingerprint_draft(story: PublicStory, references: tuple[DraftReference, ...]) -> str:
    payload = {
        "story": {
            "slug": story.slug,
            "headline": story.headline,
            "synthesis": story.synthesis,
            "why_it_matters": story.why_it_matters,
            "primary_topic": story.primary_topic.value,
            "story_type": story.story_type.value,
            "uk_relevant": story.uk_relevant,
            "first_reported_at": story.first_reported_at.isoformat(),
            "latest_material_update_at": story.latest_material_update_at.isoformat(),
        },
        "sources": [
            {
                "id": ref.source.id,
                "title": ref.source.title,
                "publisher": ref.source.publisher,
                "url": ref.source.url,
                "first_published_at": (
                    ref.source.first_published_at.isoformat()
                    if ref.source.first_published_at
                    else None
                ),
                "is_primary": ref.source.is_primary,
                "article_id": str(ref.article_id),
                "content_version_id": str(ref.content_version_id),
            }
            for ref in references
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class PublicationWriter:
    """Write through a dedicated login with the publication capability only."""

    def __init__(self, engine: Engine, *, expected_role: str) -> None:
        if not expected_role:
            raise ValueError("expected_role is required")
        self.engine = engine
        self.expected_role = expected_role

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
        fingerprint = fingerprint_draft(story, references)
        revision_id = uuid.uuid7()
        with self.engine.begin() as connection:
            assert_database_role(connection, self.expected_role)
            row = (
                connection.execute(
                    text(
                        "SELECT id, suppressed FROM primary_signal.stories WHERE slug=:slug FOR UPDATE"
                    ),
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
            connection.execute(
                text(
                    "INSERT INTO primary_signal.publication_events "
                    "(id,story_id,revision_id,from_status,to_status,actor,reason,"
                    "input_fingerprint) VALUES (:id,:story,:revision,NULL,'draft',"
                    "'system','draft created',:fingerprint)"
                ),
                {
                    "id": uuid.uuid7(),
                    "story": story_id,
                    "revision": revision_id,
                    "fingerprint": fingerprint,
                },
            )
        return story_id, revision_id, fingerprint

    def publish_reviewed(
        self,
        *,
        story_id: uuid.UUID,
        revision_id: uuid.UUID,
        input_fingerprint: str,
        decision: OperatorDecision,
    ) -> None:
        """Publish a manually reviewed draft atomically with pointer replacement.

        The operator is responsible for applying the product contract's claim,
        safety, membership, conflict and signal gates. This method cannot be
        used for unattended publication.
        """
        if len(input_fingerprint) != 64 or any(
            c not in "0123456789abcdef" for c in input_fingerprint
        ):
            raise ValueError("input fingerprint is invalid")
        with self.engine.begin() as connection:
            assert_database_role(connection, self.expected_role)
            story = (
                connection.execute(
                    text(
                        "SELECT current_revision_id,suppressed FROM primary_signal.stories "
                        "WHERE id=:story FOR UPDATE"
                    ),
                    {"story": story_id},
                )
                .mappings()
                .first()
            )
            if story is None or story["suppressed"]:
                raise PublicationConflict("story is missing or suppressed")
            revision = (
                connection.execute(
                    text(
                        "SELECT status, revision_number FROM primary_signal.story_revisions "
                        "WHERE id=:revision AND story_id=:story FOR UPDATE"
                    ),
                    {"revision": revision_id, "story": story_id},
                )
                .mappings()
                .first()
            )
            if revision is None or revision["status"] != "draft":
                raise PublicationConflict("revision is not a draft")
            latest_number = connection.execute(
                text(
                    "SELECT MAX(revision_number) FROM primary_signal.story_revisions "
                    "WHERE story_id=:story"
                ),
                {"story": story_id},
            ).scalar_one()
            if latest_number != revision["revision_number"]:
                raise PublicationConflict("a newer revision exists")
            recorded = connection.execute(
                text(
                    "SELECT input_fingerprint FROM primary_signal.publication_events "
                    "WHERE revision_id=:revision AND to_status='draft' "
                    "ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"revision": revision_id},
            ).scalar_one_or_none()
            if recorded != input_fingerprint:
                raise PublicationConflict("reviewed input fingerprint changed")
            # An immutable fetched version is required for every visible source.
            source_count = connection.execute(
                text(
                    "SELECT count(*) FROM primary_signal.revision_sources rs "
                    "JOIN primary_signal.content_versions cv ON "
                    "cv.id=rs.content_version_id AND cv.article_id=rs.article_id "
                    "JOIN primary_signal.fetch_attempts fa ON "
                    "fa.id=cv.origin_fetch_attempt_id AND fa.article_id=cv.article_id "
                    "AND fa.status='fetched' AND fa.resulting_content_version_id=cv.id "
                    "JOIN primary_signal.articles a ON a.id=cv.article_id "
                    "JOIN primary_signal.sources s ON s.id=a.source_id "
                    "WHERE rs.revision_id=:revision AND s.enabled"
                ),
                {"revision": revision_id},
            ).scalar_one()
            total_count = connection.execute(
                text(
                    "SELECT count(*) FROM primary_signal.revision_sources "
                    "WHERE revision_id=:revision"
                ),
                {"revision": revision_id},
            ).scalar_one()
            if not total_count or source_count != total_count:
                raise PublicationConflict("all visible sources require eligible fetched versions")
            old = story["current_revision_id"]
            now = datetime.now(UTC)
            connection.execute(
                text(
                    "UPDATE primary_signal.story_revisions SET status='validated' "
                    "WHERE id=:revision"
                ),
                {"revision": revision_id},
            )
            self._event(
                connection, story_id, revision_id, "draft", "validated", decision, input_fingerprint
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.story_revisions SET status='published',"
                    "published_at=:now WHERE id=:revision"
                ),
                {"revision": revision_id, "now": now},
            )
            self._event(
                connection,
                story_id,
                revision_id,
                "validated",
                "published",
                decision,
                input_fingerprint,
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.stories SET current_revision_id=:revision "
                    "WHERE id=:story"
                ),
                {"revision": revision_id, "story": story_id},
            )
            if old is not None:
                connection.execute(
                    text(
                        "UPDATE primary_signal.story_revisions SET status='superseded' "
                        "WHERE id=:old AND status='published'"
                    ),
                    {"old": old},
                )
                self._event(
                    connection,
                    story_id,
                    old,
                    "published",
                    "superseded",
                    decision,
                    input_fingerprint,
                )

    def suppress(self, *, story_id: uuid.UUID, decision: OperatorDecision) -> None:
        """Hide a current story and retain the decision in append-only history."""
        with self.engine.begin() as connection:
            assert_database_role(connection, self.expected_role)
            row = (
                connection.execute(
                    text(
                        "SELECT current_revision_id,suppressed FROM primary_signal.stories "
                        "WHERE id=:story FOR UPDATE"
                    ),
                    {"story": story_id},
                )
                .mappings()
                .first()
            )
            if row is None or row["suppressed"] or row["current_revision_id"] is None:
                raise PublicationConflict("story has no unsuppressed current revision")
            revision_id = row["current_revision_id"]
            connection.execute(
                text("UPDATE primary_signal.stories SET suppressed=true WHERE id=:story"),
                {"story": story_id},
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.story_revisions SET status='suppressed' "
                    "WHERE id=:revision AND status='published'"
                ),
                {"revision": revision_id},
            )
            self._event(
                connection, story_id, revision_id, "published", "suppressed", decision, None
            )

    @staticmethod
    def _event(
        connection: Connection,
        story_id: uuid.UUID,
        revision_id: uuid.UUID,
        from_status: str,
        to_status: str,
        decision: OperatorDecision,
        fingerprint: str | None,
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO primary_signal.publication_events "
                "(id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint) "
                "VALUES (:id,:story,:revision,:previous,:next,:actor,:reason,:fingerprint)"
            ),
            {
                "id": uuid.uuid7(),
                "story": story_id,
                "revision": revision_id,
                "previous": from_status,
                "next": to_status,
                "actor": decision.actor,
                "reason": decision.reason,
                "fingerprint": fingerprint,
            },
        )
