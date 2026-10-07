"""Feed poll job preparation with an injected, policy-enforcing fetcher."""

import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Connection, Engine

from primary_signal.ingestion.feed_parser import InvalidFeed, parse_feed
from primary_signal.ingestion.feed_polls import (
    FeedFetchResult,
    FeedPollDisabled,
    FeedPollRepository,
    FeedPollTarget,
)
from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.handlers import JobProcessingError
from primary_signal.jobs.repository import FailureDisposition, JobLease, JobRepository
from primary_signal.jobs.transactions import PreparedDatabaseCallback, PreparedFailureCallback


class FeedFetcher(Protocol):
    """Implement bounded HTTP retrieval with address and redirect policy checks."""

    def fetch(self, target: FeedPollTarget) -> FeedFetchResult: ...


class FeedFetchError(Exception):
    """An expected fetch failure with a safe job error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PollFeedHandler:
    """Prepare observations outside the queue finalisation transaction."""

    def __init__(self, engine: Engine, fetcher: FeedFetcher) -> None:
        self._engine = engine
        self._fetcher = fetcher

    def __call__(self, lease: JobLease) -> PreparedDatabaseCallback | None:
        if not isinstance(lease.payload, PollFeedV1):
            raise ValueError("poll handler received an unexpected payload")
        feed_id = lease.payload.feed_id
        with self._engine.connect() as connection:
            try:
                target = FeedPollRepository(connection).load_target(feed_id)
            except FeedPollDisabled:
                return None
        try:
            result = self._fetcher.fetch(target)
            entries = parse_feed(result.body) if result.status == 200 else ()
        except FeedFetchError as error:
            raise JobProcessingError(
                error.code,
                on_failure=self._failure_callback(feed_id, lease.job_id, target.url, error.code),
            ) from error
        except InvalidFeed as error:
            raise JobProcessingError(
                "invalid_feed",
                on_failure=self._failure_callback(
                    feed_id, lease.job_id, target.url, "invalid_feed"
                ),
            ) from error

        def record(connection: Connection, queue: JobRepository) -> None:
            FeedPollRepository(connection).record_result(
                feed_id=feed_id,
                job_id=lease.job_id,
                expected_url=target.url,
                result=result,
                entries=entries,
                now=datetime.now(UTC),
                queue=queue,
            )

        return record

    @staticmethod
    def _failure_callback(
        feed_id: uuid.UUID, job_id: uuid.UUID, expected_url: str, code: str
    ) -> PreparedFailureCallback:
        def record(
            connection: Connection, _queue: JobRepository, _disposition: FailureDisposition
        ) -> None:
            FeedPollRepository(connection).record_failure(
                feed_id=feed_id,
                job_id=job_id,
                expected_url=expected_url,
                error_code=code,
                now=datetime.now(UTC),
            )

        return record
