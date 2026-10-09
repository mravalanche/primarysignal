"""Persist bounded article observations inside a fenced job transaction."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import Connection, Table, select, update
from sqlalchemy.dialects.postgresql import insert

from primary_signal.ingestion.models import Article, ArticleUrl, ContentVersion, FetchAttempt
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.extract import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    NORMALIZATION_VERSION,
    ArticleExtraction,
)
from primary_signal.sources.models import Source

_articles = cast(Table, Article.__table__)
_urls = cast(Table, ArticleUrl.__table__)
_sources = cast(Table, Source.__table__)
_attempts = cast(Table, FetchAttempt.__table__)
_versions = cast(Table, ContentVersion.__table__)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ArticleRetrievalDisabled(ValueError):
    """The submitted article URL is no longer its enabled source's current target."""


@dataclass(frozen=True, slots=True)
class ArticleTarget:
    article_id: uuid.UUID
    article_url_id: uuid.UUID
    url: str


@dataclass(frozen=True, slots=True)
class ArticleRecordSummary:
    status: Literal["fetched", "skipped"]
    stored: bool
    content_version_id: uuid.UUID | None


def _check_times(started_at: datetime, completed_at: datetime) -> None:
    if (
        started_at.utcoffset() is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
    ):
        raise ValueError("article attempt times must be ordered and timezone-aware")


def validate_article_result(result: ArticleFetchResult) -> None:
    """Reject inconsistent retriever output before job completion is attempted."""

    if result.status != 200 or result.extraction is None:
        raise ValueError("unconditional article retrieval requires HTTP 200 content")
    extraction = result.extraction
    if any(
        (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
        for value in (extraction.title or "", extraction.text)
        for character in value
    ):
        raise ValueError("article extraction contains a database control character")
    canonical = json.dumps(
        [extraction.title, extraction.text], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(b"primary-signal/article-normalization/v1\0" + canonical).hexdigest()
    if extraction.normalized_content_hash != digest or extraction.word_count != len(
        extraction.text.split()
    ):
        raise ValueError("article extraction metadata does not match its text")


class ArticleRetrievalRepository:
    """Write one result after the caller has finalized its fenced job lease."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load_target(self, article_id: uuid.UUID, article_url_id: uuid.UUID) -> ArticleTarget:
        row = (
            self._connection.execute(
                select(
                    _urls.c.normalized_url,
                    _articles.c.current_canonical_url_id,
                    _sources.c.enabled,
                )
                .select_from(
                    _articles.join(_urls, _urls.c.article_id == _articles.c.id).join(
                        _sources, _sources.c.id == _articles.c.source_id
                    )
                )
                .where(_articles.c.id == article_id, _urls.c.id == article_url_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not row["enabled"] or row["current_canonical_url_id"] != article_url_id:
            raise ArticleRetrievalDisabled("article retrieval target is disabled or unavailable")
        return ArticleTarget(article_id, article_url_id, cast(str, row["normalized_url"]))

    def _current_target(self, target: ArticleTarget) -> bool:
        # Lock the source before the article. A shared source lock lets article
        # fetches proceed together, but serializes them with source disabling.
        source_id = self._connection.execute(
            select(_articles.c.source_id).where(_articles.c.id == target.article_id)
        ).scalar_one_or_none()
        if source_id is None:
            return False
        source_enabled = self._connection.execute(
            select(_sources.c.enabled).where(_sources.c.id == source_id).with_for_update(read=True)
        ).scalar_one_or_none()
        if not source_enabled:
            return False
        article = (
            self._connection.execute(
                select(_articles.c.source_id, _articles.c.current_canonical_url_id)
                .where(_articles.c.id == target.article_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            article is None
            or article["source_id"] != source_id
            or article["current_canonical_url_id"] != target.article_url_id
        ):
            return False
        url = self._connection.execute(
            select(_urls.c.normalized_url).where(
                _urls.c.id == target.article_url_id,
                _urls.c.article_id == target.article_id,
            )
        ).scalar_one_or_none()
        return url == target.url

    def record_result(
        self,
        *,
        target: ArticleTarget,
        job_id: uuid.UUID,
        result: ArticleFetchResult,
        started_at: datetime,
        completed_at: datetime,
    ) -> ArticleRecordSummary:
        _check_times(started_at, completed_at)
        validate_article_result(result)
        if not self._current_target(target):
            return ArticleRecordSummary("skipped", False, None)
        extraction = cast(ArticleExtraction, result.extraction)  # Checked before any writes.
        attempt_id = uuid.uuid7()
        self._connection.execute(
            insert(_attempts).values(
                id=attempt_id,
                article_id=target.article_id,
                job_id=job_id,
                retrieval_strategy="direct_http",
                requested_url=target.url,
                final_url=result.final_url,
                redirect_chain=[
                    {"status": hop.status, "url": hop.url} for hop in result.redirect_chain
                ],
                status="running",
                started_at=started_at,
            )
        )
        version_id = uuid.uuid7()
        inserted_id = self._connection.execute(
            insert(_versions)
            .values(
                id=version_id,
                article_id=target.article_id,
                origin_fetch_attempt_id=attempt_id,
                raw_response_hash=result.raw_response_hash,
                normalized_content_hash=extraction.normalized_content_hash,
                normalization_version=NORMALIZATION_VERSION,
                extracted_title=extraction.title,
                extracted_text=extraction.text,
                extractor_name=EXTRACTOR_NAME,
                extractor_version=EXTRACTOR_VERSION,
                content_type=result.content_type,
                word_count=extraction.word_count,
                fetched_at=completed_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    _versions.c.article_id,
                    _versions.c.normalization_version,
                    _versions.c.normalized_content_hash,
                ]
            )
            .returning(_versions.c.id)
        ).scalar_one_or_none()
        if inserted_id is None:
            version_id = self._connection.execute(
                select(_versions.c.id).where(
                    _versions.c.article_id == target.article_id,
                    _versions.c.normalization_version == NORMALIZATION_VERSION,
                    _versions.c.normalized_content_hash == extraction.normalized_content_hash,
                )
            ).scalar_one()
        else:
            version_id = inserted_id
        self._connection.execute(
            update(_attempts)
            .where(_attempts.c.id == attempt_id)
            .values(
                status="fetched",
                completed_at=completed_at,
                http_status=result.status,
                content_type=result.content_type,
                byte_count=result.decoded_byte_count,
                returned_etag=result.etag,
                returned_last_modified=result.last_modified,
                resulting_content_version_id=version_id,
            )
        )
        self._connection.execute(
            update(_articles)
            .where(_articles.c.id == target.article_id)
            .values(current_content_version_id=version_id)
        )
        return ArticleRecordSummary("fetched", inserted_id is not None, version_id)

    def record_failure(
        self,
        *,
        target: ArticleTarget,
        job_id: uuid.UUID,
        error_code: str,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        _check_times(started_at, completed_at)
        if not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("invalid article retrieval error code")
        if not self._current_target(target):
            return
        attempt_id = uuid.uuid7()
        self._connection.execute(
            insert(_attempts).values(
                id=attempt_id,
                article_id=target.article_id,
                job_id=job_id,
                retrieval_strategy="direct_http",
                requested_url=target.url,
                redirect_chain=[],
                status="running",
                started_at=started_at,
            )
        )
        self._connection.execute(
            update(_attempts)
            .where(_attempts.c.id == attempt_id)
            .values(status="failed", completed_at=completed_at, error_code=error_code)
        )
