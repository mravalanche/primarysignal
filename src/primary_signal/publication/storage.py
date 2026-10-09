"""Private publication snapshots; public applications query views instead."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from primary_signal.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Story(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Stable public identity and the current publication pointer."""

    __tablename__ = "stories"
    __table_args__ = (
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(slug) <= 160",
            name="valid_slug",
        ),
        UniqueConstraint("slug"),
        UniqueConstraint("id", "current_revision_id", name="uq_stories_id_current"),
        ForeignKeyConstraint(
            ["id", "current_revision_id"],
            ["primary_signal.story_revisions.story_id", "primary_signal.story_revisions.id"],
            name="fk_stories_current_same_story",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index(
            "ix_stories_current",
            "current_revision_id",
            postgresql_where=text("current_revision_id IS NOT NULL AND NOT suppressed"),
        ),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column()
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class StoryRevision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Editorial snapshot with a controlled lifecycle."""

    __tablename__ = "story_revisions"
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="positive_revision_number"),
        CheckConstraint(
            "status IN ('draft','validated','published','suppressed','superseded')",
            name="valid_status",
        ),
        CheckConstraint("char_length(headline) BETWEEN 1 AND 300", name="valid_headline"),
        CheckConstraint("char_length(synthesis) > 0", name="nonempty_synthesis"),
        CheckConstraint("char_length(why_it_matters) > 0", name="nonempty_importance"),
        CheckConstraint(
            "primary_topic IN ('vulnerabilities-and-exploitation',"
            "'threat-activity-and-incidents','security-engineering',"
            "'policy-and-strategy','research-and-tools')",
            name="valid_topic",
        ),
        CheckConstraint(
            "story_type IN ('news','research','advisory','incident',"
            "'analysis','opinion','tool-release')",
            name="valid_story_type",
        ),
        CheckConstraint(
            "latest_material_update_at >= first_reported_at", name="material_time_order"
        ),
        CheckConstraint(
            "(status IN ('published','superseded','suppressed')) = (published_at IS NOT NULL)",
            name="publication_time",
        ),
        UniqueConstraint("story_id", "id"),
        UniqueConstraint("story_id", "revision_number"),
        Index("ix_story_revisions_story_status", "story_id", "status"),
    )

    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.stories.id", ondelete="RESTRICT"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    synthesis: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[str] = mapped_column(Text, nullable=False)
    primary_topic: Mapped[str] = mapped_column(Text, nullable=False)
    story_type: Mapped[str] = mapped_column(Text, nullable=False)
    uk_relevant: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    first_reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_material_update_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RevisionSource(Base):
    """Public source presentation plus private exact input version reference."""

    __tablename__ = "revision_sources"
    __table_args__ = (
        CheckConstraint(
            "source_id ~ '^[a-z0-9]+([._-][a-z0-9]+)*$' AND char_length(source_id) <= 160",
            name="valid_source_id",
        ),
        CheckConstraint("position > 0", name="positive_position"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 300", name="valid_title"),
        CheckConstraint("char_length(publisher) BETWEEN 1 AND 160", name="valid_publisher"),
        CheckConstraint(
            "public_url ~ '^https?://' AND char_length(public_url) <= 2048",
            name="public_url_shape",
        ),
        CheckConstraint("(article_id IS NULL) = (content_version_id IS NULL)", name="version_pair"),
        ForeignKeyConstraint(
            ["article_id", "content_version_id"],
            ["primary_signal.content_versions.article_id", "primary_signal.content_versions.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("revision_id", "position"),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.story_revisions.id", ondelete="RESTRICT"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str] = mapped_column(Text, nullable=False)
    public_url: Mapped[str] = mapped_column(Text, nullable=False)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    article_id: Mapped[uuid.UUID | None] = mapped_column()
    content_version_id: Mapped[uuid.UUID | None] = mapped_column()


class Tag(Base):
    """Canonical tag, exposed only when a current story uses it."""

    __tablename__ = "tags"
    __table_args__ = (
        CheckConstraint(
            "id ~ '^[a-z0-9]+([._-][a-z0-9]+)*$' AND char_length(id) <= 160",
            name="valid_id",
        ),
        CheckConstraint("char_length(label) BETWEEN 1 AND 120", name="valid_label"),
        CheckConstraint(
            "kind IN ('cve','organisation','product','actor','technology','sector','curated')",
            name="valid_kind",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)


class RevisionTag(Base):
    """Canonical tag membership frozen with a revision."""

    __tablename__ = "revision_tags"
    __table_args__ = (
        CheckConstraint("position > 0", name="positive_position"),
        UniqueConstraint("revision_id", "position"),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.story_revisions.id", ondelete="RESTRICT"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        ForeignKey("primary_signal.tags.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class RevisionSignal(Base):
    """Named claim whose evidence must refer to snapshot sources."""

    __tablename__ = "revision_signals"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('primary-source','official-advisory','active-exploitation',"
            "'exploit-available','actionable','confirmed-incident','developing',"
            "'widely-reported','deep-read')",
            name="valid_kind",
        ),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.story_revisions.id", ondelete="RESTRICT"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)


class SignalEvidence(Base):
    """A signal's exact public source reference."""

    __tablename__ = "signal_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "kind"],
            ["primary_signal.revision_signals.revision_id", "primary_signal.revision_signals.kind"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["revision_id", "source_id"],
            [
                "primary_signal.revision_sources.revision_id",
                "primary_signal.revision_sources.source_id",
            ],
            ondelete="RESTRICT",
        ),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)


class PublicationEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only record of a publication decision."""

    __tablename__ = "publication_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["story_id", "revision_id"],
            ["primary_signal.story_revisions.story_id", "primary_signal.story_revisions.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "to_status IN ('draft','validated','published','suppressed','superseded')",
            name="valid_to_status",
        ),
        CheckConstraint("char_length(actor) BETWEEN 1 AND 160", name="valid_actor"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 1024", name="valid_reason"),
        CheckConstraint(
            "input_fingerprint IS NULL OR input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="valid_fingerprint",
        ),
        Index("ix_publication_events_revision_time", "revision_id", "occurred_at"),
    )

    story_id: Mapped[uuid.UUID] = mapped_column()
    revision_id: Mapped[uuid.UUID] = mapped_column()
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    input_fingerprint: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
