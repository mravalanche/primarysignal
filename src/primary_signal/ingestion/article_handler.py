"""Prepare article observations for fenced, transactional persistence."""

import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Connection, Engine

from primary_signal.ingestion.article_results import (
    ArticleRetrievalDisabled,
    ArticleRetrievalRepository,
    ArticleTarget,
    validate_article_result,
)
from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.jobs.contracts import RetrieveArticleV1
from primary_signal.jobs.handlers import JobProcessingError
from primary_signal.jobs.repository import FailureDisposition, JobLease, JobRepository
from primary_signal.jobs.transactions import PreparedDatabaseCallback, PreparedFailureCallback
from primary_signal.retrieval.article import ArticleFetchResult


class ArticleFetcher(Protocol):
    """A client for the isolated, address-pinned article retriever."""

    def fetch_article(self, url: str) -> ArticleFetchResult: ...


class ArticleRetrievalHandler:
    """Fetch outside a database transaction, then prepare a fenced callback."""

    def __init__(self, engine: Engine, fetcher: ArticleFetcher) -> None:
        self._engine = engine
        self._fetcher = fetcher

    def __call__(self, lease: JobLease) -> PreparedDatabaseCallback | None:
        if not isinstance(lease.payload, RetrieveArticleV1):
            raise ValueError("article handler received an unexpected payload")
        with self._engine.connect() as connection:
            try:
                target = ArticleRetrievalRepository(connection).load_target(
                    lease.payload.article_id, lease.payload.article_url_id
                )
            except ArticleRetrievalDisabled:
                return None

        started_at = datetime.now(UTC)
        try:
            result = self._fetcher.fetch_article(target.url)
            # The first retrieval has no saved per-URL validators. A 304 is
            # therefore unusable and must never become a content version.
            if result.status == 304:
                raise FeedFetchError("invalid_response")
            try:
                validate_article_result(result)
            except ValueError as error:
                raise FeedFetchError("retriever_protocol") from error
        except FeedFetchError as error:
            raise JobProcessingError(
                error.code,
                on_failure=self._failure_callback(target, lease.job_id, error.code, started_at),
            ) from error

        def record(connection: Connection, _queue: JobRepository) -> None:
            ArticleRetrievalRepository(connection).record_result(
                target=target,
                job_id=lease.job_id,
                result=result,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )

        return record

    @staticmethod
    def _failure_callback(
        target: ArticleTarget, job_id: uuid.UUID, error_code: str, started_at: datetime
    ) -> PreparedFailureCallback:
        def record(
            connection: Connection, _queue: JobRepository, _disposition: FailureDisposition
        ) -> None:
            ArticleRetrievalRepository(connection).record_failure(
                target=target,
                job_id=job_id,
                error_code=error_code,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )

        return record
