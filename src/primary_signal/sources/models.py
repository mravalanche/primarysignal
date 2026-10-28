"""Publisher source and feed persistence models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from primary_signal.db.base import Base, UpdatedAtMixin, UUIDPrimaryKeyMixin


class Source(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    """An editorially configured publisher or upstream authority."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("source_key ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="valid_key"),
    )

    source_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage_url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    feeds: Mapped[list[Feed]] = relationship(back_populates="source")


class Feed(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    """A polling target with explicit URL-normalisation identity."""

    __tablename__ = "feeds"
    __table_args__ = (
        CheckConstraint("char_length(url_hash) = 64", name="url_hash_length"),
        CheckConstraint("url_normalization_version > 0", name="positive_url_version"),
        CheckConstraint("poll_interval_seconds >= 60", name="minimum_poll_interval"),
        CheckConstraint("consecutive_failures >= 0", name="nonnegative_failures"),
        CheckConstraint(
            "last_success_at IS NULL OR last_attempt_at IS NULL OR last_success_at <= last_attempt_at",
            name="poll_time_order",
        ),
        UniqueConstraint("url_normalization_version", "url_hash"),
        Index("ix_feeds_source_id", "source_id"),
        Index("ix_feeds_due", "next_poll_at", "id", postgresql_where=text("enabled")),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.sources.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    configured_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    url_normalization_version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=900, server_default="900"
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source: Mapped[Source] = relationship(back_populates="feeds")


class SourceStateEvent(UUIDPrimaryKeyMixin, Base):
    """An immutable, database-attributed source pause or resume decision."""

    __tablename__ = "source_state_events"
    __table_args__ = (
        CheckConstraint("old_enabled <> new_enabled", name="changed_state"),
        CheckConstraint(
            r"char_length(reason) BETWEEN 1 AND 500 AND btrim(reason, U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000') = reason AND reason !~ U&'[\0001-\001F\007F-\009F]'",
            name="valid_reason",
        ),
        CheckConstraint("char_length(actor) > 0", name="valid_actor"),
        Index("ix_source_state_events_source_time", "source_id", "occurred_at", "id"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.sources.id", ondelete="RESTRICT"), nullable=False
    )
    old_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    new_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
