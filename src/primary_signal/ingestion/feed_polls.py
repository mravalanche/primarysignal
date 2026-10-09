"""Transactional persistence of prepared feed poll observations."""

import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from sqlalchemy import Connection, Table, func, select, update
from sqlalchemy.dialects.postgresql import insert

from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.feed_parser import MAX_FEED_BYTES, ParsedFeedEntry
from primary_signal.ingestion.models import Article, ArticleUrl, FeedEntry, FeedPollRun
from primary_signal.jobs.contracts import RetrieveArticleV1
from primary_signal.jobs.repository import JobRepository
from primary_signal.sources.models import Feed, Source

_feeds = cast(Table, Feed.__table__)
_sources = cast(Table, Source.__table__)
_runs = cast(Table, FeedPollRun.__table__)
_entries = cast(Table, FeedEntry.__table__)
_articles = cast(Table, Article.__table__)
_urls = cast(Table, ArticleUrl.__table__)


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    """A bounded response produced by a separate, policy-enforcing HTTP client."""

    status: int
    final_url: str = field(repr=False)
    body: bytes = field(repr=False)
    etag: str | None = field(default=None, repr=False)
    last_modified: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in (200, 304):
            raise ValueError("feed fetch result must be HTTP 200 or 304")
        if len(self.body) > MAX_FEED_BYTES or (self.status == 304 and self.body):
            raise ValueError("feed response body exceeds its bound or is invalid for 304")
        identify_url(self.final_url)
        if any(
            value is not None and len(value) > 4096 for value in (self.etag, self.last_modified)
        ):
            raise ValueError("feed validator exceeds its bound")
        if any(
            value is not None
            and any(unicodedata.category(character).startswith("C") for character in value)
            for value in (self.etag, self.last_modified)
        ):
            raise ValueError("feed validator contains a control character")


@dataclass(frozen=True, slots=True)
class FeedPollSummary:
    seen: int
    discovered: int
    retrieval_jobs: int


class FeedPollDisabled(ValueError):
    """The configured feed or its source is no longer enabled."""


@dataclass(frozen=True, slots=True)
class FeedPollTarget:
    feed_id: uuid.UUID
    url: str = field(repr=False)
    etag: str | None = field(repr=False)
    last_modified: str | None = field(repr=False)


class FeedPollRepository:
    """Write one result in a caller-owned, fenced job transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load_target(self, feed_id: uuid.UUID) -> FeedPollTarget:
        """Read current fetch parameters before external work begins."""

        row = (
            self._connection.execute(
                select(
                    _feeds.c.id,
                    _feeds.c.normalized_url,
                    _feeds.c.etag,
                    _feeds.c.last_modified,
                    _feeds.c.enabled,
                    _sources.c.enabled.label("source_enabled"),
                )
                .join(_sources, _sources.c.id == _feeds.c.source_id)
                .where(_feeds.c.id == feed_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not row["enabled"] or not row["source_enabled"]:
            raise FeedPollDisabled("feed is disabled or unavailable")
        return FeedPollTarget(
            feed_id=feed_id,
            url=row["normalized_url"],
            etag=row["etag"],
            last_modified=row["last_modified"],
        )

    def record_result(
        self,
        *,
        feed_id: uuid.UUID,
        job_id: uuid.UUID,
        expected_url: str,
        result: FeedFetchResult,
        entries: tuple[ParsedFeedEntry, ...],
        now: datetime,
        queue: JobRepository,
    ) -> FeedPollSummary:
        if now.tzinfo is None:
            raise ValueError("poll time must have a timezone")
        if result.status == 304 and entries:
            raise ValueError("HTTP 304 cannot contain parsed entries")
        feed = (
            self._connection.execute(
                select(
                    _feeds.c.id,
                    _feeds.c.source_id,
                    _feeds.c.configured_url,
                    _feeds.c.normalized_url,
                    _feeds.c.etag,
                    _feeds.c.last_modified,
                    _feeds.c.enabled,
                )
                .where(_feeds.c.id == feed_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if feed is None:
            return FeedPollSummary(seen=0, discovered=0, retrieval_jobs=0)
        source_enabled = self._connection.execute(
            select(func.primary_signal.lock_source_enabled(feed["source_id"]))
        ).scalar_one()
        if not feed["enabled"] or not source_enabled or feed["normalized_url"] != expected_url:
            return FeedPollSummary(seen=0, discovered=0, retrieval_jobs=0)
        run_id = uuid.uuid7()
        self._connection.execute(
            insert(_runs).values(
                id=run_id,
                feed_id=feed_id,
                job_id=job_id,
                requested_url=feed["configured_url"],
                status="running",
                started_at=now,
            )
        )
        discovered = 0
        jobs = 0
        if result.status == 200:
            # Consistent URL lock order prevents feeds sharing several articles
            # from waiting on one another in opposite orders.
            ordered_entries = sorted(
                entries,
                key=lambda entry: (
                    identify_url(entry.reported_url).url_hash if entry.reported_url else "",
                    entry.identity_key,
                ),
            )
            for entry in ordered_entries:
                inserted = self._connection.execute(
                    insert(_entries)
                    .values(
                        id=uuid.uuid7(),
                        feed_id=feed_id,
                        first_poll_run_id=run_id,
                        identity_method=entry.identity_method,
                        identity_version=1,
                        identity_key=entry.identity_key,
                        reported_guid=entry.reported_guid,
                        reported_url=entry.reported_url,
                        reported_title=entry.reported_title,
                        reported_summary=entry.reported_summary,
                        reported_author=entry.reported_author,
                        reported_published_at=entry.reported_published_at,
                        reported_updated_at=entry.reported_updated_at,
                        metadata_hash=entry.metadata_hash,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            _entries.c.feed_id,
                            _entries.c.identity_version,
                            _entries.c.identity_key,
                        ]
                    )
                    .returning(_entries.c.id)
                ).scalar_one_or_none()
                if inserted is None:
                    article_id = self._connection.execute(
                        update(_entries)
                        .where(
                            _entries.c.feed_id == feed_id,
                            _entries.c.identity_version == 1,
                            _entries.c.identity_key == entry.identity_key,
                        )
                        .values(last_seen_at=func.greatest(_entries.c.last_seen_at, now))
                        .returning(_entries.c.article_id)
                    ).scalar_one()
                    if article_id is not None:
                        self._connection.execute(
                            update(_articles)
                            .where(_articles.c.id == article_id)
                            .values(last_seen_at=func.greatest(_articles.c.last_seen_at, now))
                        )
                    continue
                discovered += 1
                if entry.reported_url is None:
                    continue
                identity = identify_url(entry.reported_url)
                # Every writer of article URL identities uses the same advisory lock.
                # This keeps creation idempotent across feeds without an orphan row.
                lock_key = int(identity.url_hash[:16], 16)
                if lock_key >= 2**63:
                    lock_key -= 2**64
                self._connection.execute(select(func.pg_advisory_xact_lock(lock_key)))
                existing = self._connection.execute(
                    select(_urls.c.article_id, _urls.c.id).where(
                        _urls.c.normalization_version == identity.normalization_version,
                        _urls.c.normalized_url_hash == identity.url_hash,
                    )
                ).one_or_none()
                if existing is None:
                    article_id = uuid.uuid7()
                    self._connection.execute(
                        insert(_articles).values(
                            id=article_id,
                            source_id=feed["source_id"],
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                    url_id = uuid.uuid7()
                    new_url = self._connection.execute(
                        insert(_urls)
                        .values(
                            id=url_id,
                            article_id=article_id,
                            original_url=entry.reported_url,
                            normalized_url=identity.normalized_url,
                            normalized_url_hash=identity.url_hash,
                            normalization_version=identity.normalization_version,
                            kind="submitted",
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                        .returning(_urls.c.id)
                    ).scalar_one()
                    self._connection.execute(
                        update(_articles)
                        .where(_articles.c.id == article_id)
                        .values(current_canonical_url_id=new_url)
                    )
                    existing = (article_id, new_url)
                article_id, url_id = existing
                self._connection.execute(
                    update(_entries).where(_entries.c.id == inserted).values(article_id=article_id)
                )
                self._connection.execute(
                    update(_articles)
                    .where(_articles.c.id == article_id)
                    .values(last_seen_at=func.greatest(_articles.c.last_seen_at, now))
                )
                self._connection.execute(
                    update(_urls)
                    .where(_urls.c.id == url_id)
                    .values(last_seen_at=func.greatest(_urls.c.last_seen_at, now))
                )
                queued = queue.enqueue(
                    job_type="articles.retrieve",
                    payload_version=1,
                    payload=RetrieveArticleV1(article_id=article_id, article_url_id=url_id),
                    deduplication_key=f"article:{article_id}",
                )
                jobs += int(queued.created)
        self._connection.execute(
            update(_runs)
            .where(_runs.c.id == run_id)
            .values(
                status="succeeded" if result.status == 200 else "not_modified",
                completed_at=now,
                http_status=result.status,
                entries_seen=len(entries),
                entries_discovered=discovered,
                returned_etag=result.etag,
                returned_last_modified=result.last_modified,
            )
        )
        self._connection.execute(
            update(_feeds)
            .where(_feeds.c.id == feed_id)
            .values(
                etag=result.etag if result.etag is not None else feed["etag"],
                last_modified=(
                    result.last_modified
                    if result.last_modified is not None
                    else feed["last_modified"]
                ),
                last_attempt_at=now,
                last_success_at=now,
                consecutive_failures=0,
                updated_at=now,
            )
        )
        return FeedPollSummary(seen=len(entries), discovered=discovered, retrieval_jobs=jobs)

    def record_failure(
        self,
        *,
        feed_id: uuid.UUID,
        job_id: uuid.UUID,
        expected_url: str,
        error_code: str,
        now: datetime,
    ) -> None:
        """Record a stable failure code; never persist raw exceptions or bodies."""

        feed = (
            self._connection.execute(
                select(
                    _feeds.c.id,
                    _feeds.c.configured_url,
                    _feeds.c.normalized_url,
                    _feeds.c.consecutive_failures,
                    _feeds.c.enabled,
                    _feeds.c.source_id,
                )
                .where(_feeds.c.id == feed_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if feed is None:
            return
        if not feed["enabled"] or feed["normalized_url"] != expected_url:
            return
        source_enabled = self._connection.execute(
            select(func.primary_signal.lock_source_enabled(feed["source_id"]))
        ).scalar_one()
        if not source_enabled:
            return
        self._connection.execute(
            insert(_runs).values(
                id=uuid.uuid7(),
                feed_id=feed_id,
                job_id=job_id,
                requested_url=feed["configured_url"],
                status="failed",
                started_at=now,
                completed_at=now,
                error_code=error_code,
            )
        )
        self._connection.execute(
            update(_feeds)
            .where(_feeds.c.id == feed_id)
            .values(
                last_attempt_at=now,
                consecutive_failures=_feeds.c.consecutive_failures + 1,
                updated_at=now,
            )
        )
