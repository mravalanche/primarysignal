"""Read-only, bounded preview of extracted text eligible under ADR 0010."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, exists, func, select, text

from primary_signal.ingestion.models import Article, ContentVersion, ContentVersionRetention
from primary_signal.publication.storage import RevisionSource, StoryRevision


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    article_id: uuid.UUID
    content_version_id: uuid.UUID
    superseded_at: datetime


def list_retention_candidates(
    connection: Connection, *, limit: int = 100
) -> tuple[RetentionCandidate, ...]:
    """Preview old unreferenced versions without selecting their article text.

    A historical publication is any cited revision with a non-null published_at;
    suppression or supersession does not remove that protection. This preview
    is advisory: a future cleanup transaction must lock and recheck each row.
    """

    if not 1 <= limit <= 500:
        raise ValueError("retention inventory limit must be between 1 and 500")

    historical_publication = exists(
        select(RevisionSource.revision_id)
        .join(StoryRevision, StoryRevision.id == RevisionSource.revision_id)
        .where(
            RevisionSource.article_id == ContentVersion.article_id,
            RevisionSource.content_version_id == ContentVersion.id,
            StoryRevision.published_at.is_not(None),
        )
    )
    statement = (
        select(
            ContentVersion.article_id,
            ContentVersion.id,
            ContentVersionRetention.superseded_at,
        )
        .join(
            ContentVersionRetention,
            ContentVersionRetention.content_version_id == ContentVersion.id,
        )
        .join(Article, Article.id == ContentVersion.article_id)
        .where(
            and_(
                Article.current_content_version_id.is_distinct_from(ContentVersion.id),
                ContentVersion.extracted_text.is_not(None),
                ContentVersionRetention.superseded_at
                <= func.clock_timestamp() - text("interval '90 days'"),
                ~historical_publication,
            )
        )
        .order_by(ContentVersionRetention.superseded_at, ContentVersion.id)
        .limit(limit)
    )
    return tuple(RetentionCandidate(*row) for row in connection.execute(statement).all())
