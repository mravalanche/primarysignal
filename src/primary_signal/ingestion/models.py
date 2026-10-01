"""Feed observations, fetch history, and immutable content versions."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from primary_signal.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class FeedPollRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One durable feed poll with the target and returned validators preserved."""

    __tablename__ = "feed_poll_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'not_modified', 'failed')", name="valid_status"
        ),
        CheckConstraint(
            "(completed_at IS NULL AND status = 'running') OR (completed_at IS NOT NULL AND status <> 'running')",
            name="completed_lifecycle",
        ),
        CheckConstraint("completed_at IS NULL OR completed_at >= started_at", name="time_order"),
        CheckConstraint("entries_seen IS NULL OR entries_seen >= 0", name="nonnegative_seen"),
        CheckConstraint(
            "entries_discovered IS NULL OR entries_discovered >= 0", name="nonnegative_discovered"
        ),
        CheckConstraint(
            "entries_discovered IS NULL OR entries_seen IS NULL OR entries_discovered <= entries_seen",
            name="entry_count_order",
        ),
        CheckConstraint(
            "error_detail IS NULL OR char_length(error_detail) <= 2048", name="bounded_error_detail"
        ),
        Index("ix_feed_poll_runs_feed_started", "feed_id", "started_at"),
        UniqueConstraint("feed_id", "id"),
    )

    feed_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.feeds.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("primary_signal.jobs.id", ondelete="RESTRICT")
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="running", server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    entries_seen: Mapped[int | None] = mapped_column(Integer)
    entries_discovered: Mapped[int | None] = mapped_column(Integer)
    returned_etag: Mapped[str | None] = mapped_column(Text)
    returned_last_modified: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)


class FeedEntry(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An item observed in one feed, preserving its first reported values."""

    __tablename__ = "feed_entries"
    __table_args__ = (
        CheckConstraint(
            "identity_method IN ('guid', 'url', 'fingerprint')", name="valid_identity_method"
        ),
        CheckConstraint("identity_version > 0", name="positive_identity_version"),
        CheckConstraint("char_length(identity_key) = 64", name="identity_key_hash_length"),
        CheckConstraint("char_length(metadata_hash) = 64", name="metadata_hash_length"),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_time_order"),
        ForeignKeyConstraint(
            ["feed_id", "first_poll_run_id"],
            ["primary_signal.feed_poll_runs.feed_id", "primary_signal.feed_poll_runs.id"],
            name="fk_feed_entries_first_poll_same_feed",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("feed_id", "identity_version", "identity_key"),
        Index("ix_feed_entries_feed_last_seen", "feed_id", "last_seen_at"),
        Index("ix_feed_entries_article_id", "article_id"),
    )

    feed_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.feeds.id", ondelete="RESTRICT"), nullable=False
    )
    first_poll_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("primary_signal.articles.id", ondelete="RESTRICT")
    )
    identity_method: Mapped[str] = mapped_column(Text, nullable=False)
    identity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    reported_guid: Mapped[str | None] = mapped_column(Text)
    reported_url: Mapped[str | None] = mapped_column(Text)
    reported_title: Mapped[str | None] = mapped_column(Text)
    reported_summary: Mapped[str | None] = mapped_column(Text)
    reported_author: Mapped[str | None] = mapped_column(Text)
    reported_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_hash: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Article(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A stable source document with same-article current pointers."""

    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_time_order"),
        ForeignKeyConstraint(
            ["id", "current_canonical_url_id"],
            ["primary_signal.article_urls.article_id", "primary_signal.article_urls.id"],
            name="fk_articles_current_url_same_article",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["id", "current_content_version_id"],
            ["primary_signal.content_versions.article_id", "primary_signal.content_versions.id"],
            name="fk_articles_current_content_same_article",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index("ix_articles_source_last_seen", "source_id", "last_seen_at"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.sources.id", ondelete="RESTRICT"), nullable=False
    )
    current_canonical_url_id: Mapped[uuid.UUID | None] = mapped_column()
    current_content_version_id: Mapped[uuid.UUID | None] = mapped_column()
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArticleUrl(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An original and normalized URL observed for an article."""

    __tablename__ = "article_urls"
    __table_args__ = (
        CheckConstraint("char_length(normalized_url_hash) = 64", name="url_hash_length"),
        CheckConstraint("normalization_version > 0", name="positive_normalization_version"),
        CheckConstraint(
            "kind IN ('submitted', 'redirect', 'canonical', 'canonical-hint')", name="valid_kind"
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_time_order"),
        UniqueConstraint("article_id", "id"),
        UniqueConstraint("normalization_version", "normalized_url_hash"),
        Index("ix_article_urls_article_kind", "article_id", "kind"),
    )

    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.articles.id", ondelete="RESTRICT"), nullable=False
    )
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    normalization_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FetchAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A bounded retrieval record without a raw response body."""

    __tablename__ = "fetch_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'fetched', 'not_modified', 'rejected', 'failed')",
            name="valid_status",
        ),
        CheckConstraint(
            "(completed_at IS NULL AND status = 'running') OR (completed_at IS NOT NULL AND status <> 'running')",
            name="completed_lifecycle",
        ),
        CheckConstraint("completed_at IS NULL OR completed_at >= started_at", name="time_order"),
        CheckConstraint("byte_count IS NULL OR byte_count >= 0", name="nonnegative_bytes"),
        CheckConstraint("jsonb_typeof(redirect_chain) = 'array'", name="redirect_array"),
        CheckConstraint(
            "error_detail IS NULL OR char_length(error_detail) <= 2048", name="bounded_error_detail"
        ),
        ForeignKeyConstraint(
            ["article_id", "resulting_content_version_id"],
            ["primary_signal.content_versions.article_id", "primary_signal.content_versions.id"],
            name="fk_fetch_attempts_result_same_article",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint("article_id", "id"),
        Index("ix_fetch_attempts_article_started", "article_id", "started_at"),
    )

    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.articles.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("primary_signal.jobs.id", ondelete="RESTRICT")
    )
    retrieval_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(Text)
    byte_count: Mapped[int | None] = mapped_column(BigInteger)
    returned_etag: Mapped[str | None] = mapped_column(Text)
    returned_last_modified: Mapped[str | None] = mapped_column(Text)
    resulting_content_version_id: Mapped[uuid.UUID | None] = mapped_column()
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)


class ContentVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An immutable extracted article revision; raw response bodies are excluded."""

    __tablename__ = "content_versions"
    __table_args__ = (
        CheckConstraint("char_length(raw_response_hash) = 64", name="raw_hash_length"),
        CheckConstraint("char_length(normalized_content_hash) = 64", name="content_hash_length"),
        CheckConstraint("normalization_version > 0", name="positive_normalization_version"),
        CheckConstraint("word_count IS NULL OR word_count >= 0", name="nonnegative_word_count"),
        UniqueConstraint("article_id", "id"),
        UniqueConstraint("article_id", "normalization_version", "normalized_content_hash"),
        ForeignKeyConstraint(
            ["article_id", "origin_fetch_attempt_id"],
            ["primary_signal.fetch_attempts.article_id", "primary_signal.fetch_attempts.id"],
            name="fk_content_versions_origin_fetch_same_article",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("origin_fetch_attempt_id"),
        Index("ix_content_versions_article_fetched", "article_id", "fetched_at"),
    )

    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.articles.id", ondelete="RESTRICT"), nullable=False
    )
    origin_fetch_attempt_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    raw_response_hash: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    normalization_version: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_title: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extractor_name: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
