"""The poll handler prepares work without owning a database transaction."""

import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import Connection, Engine

from primary_signal.ingestion.feed_handler import FeedFetchError, PollFeedHandler
from primary_signal.ingestion.feed_polls import FeedFetchResult, FeedPollDisabled, FeedPollTarget
from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.handlers import JobProcessingError
from primary_signal.jobs.repository import FailureDisposition, JobLease, JobRepository


def _lease() -> JobLease:
    return JobLease(
        job_id=uuid.uuid7(),
        attempt_id=uuid.uuid7(),
        attempt_number=1,
        lease_token=uuid.uuid7(),
        worker_id=f"processor:{uuid.uuid4()}",
        lease_expires_at=datetime.now(UTC),
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
    )


def _engine() -> Engine:
    return cast(Engine, MagicMock())


def test_success_prepares_fenced_poll_persistence() -> None:
    lease = _lease()
    target = FeedPollTarget(
        feed_id=cast(PollFeedV1, lease.payload).feed_id,
        url="https://public.example/feed",
        etag=None,
        last_modified=None,
    )
    fetcher = Mock()
    fetcher.fetch.return_value = FeedFetchResult(
        status=200,
        final_url=target.url,
        body=b"<rss><channel><item><guid>a</guid><link>https://public.example/a</link></item></channel></rss>",
    )
    with patch("primary_signal.ingestion.feed_handler.FeedPollRepository") as repository:
        repository.return_value.load_target.return_value = target
        callback = PollFeedHandler(_engine(), fetcher)(lease)
        assert callback is not None
        fetcher.fetch.assert_called_once_with(target)
        assert repository.return_value.record_result.call_count == 0
        callback(cast(Connection, Mock()), cast(JobRepository, Mock()))
        arguments = repository.return_value.record_result.call_args.kwargs
        assert arguments["feed_id"] == target.feed_id
        assert arguments["expected_url"] == target.url
        assert len(arguments["entries"]) == 1


def test_fetch_error_prepares_stable_failure_record() -> None:
    lease = _lease()
    target = FeedPollTarget(
        feed_id=cast(PollFeedV1, lease.payload).feed_id,
        url="https://public.example/feed",
        etag=None,
        last_modified=None,
    )
    fetcher = Mock()
    fetcher.fetch.side_effect = FeedFetchError("dependency_timeout")
    with patch("primary_signal.ingestion.feed_handler.FeedPollRepository") as repository:
        repository.return_value.load_target.return_value = target
        with pytest.raises(JobProcessingError) as raised:
            PollFeedHandler(_engine(), fetcher)(lease)
        assert raised.value.failure.code == "dependency_timeout"
        assert raised.value.on_failure is not None
        raised.value.on_failure(
            cast(Connection, Mock()),
            cast(JobRepository, Mock()),
            FailureDisposition(status="dead", retry_at=None),
        )
        arguments = repository.return_value.record_failure.call_args.kwargs
        assert arguments["error_code"] == "dependency_timeout"
        assert arguments["expected_url"] == target.url


def test_disabled_feed_skips_fetch() -> None:
    fetcher = Mock()
    with patch("primary_signal.ingestion.feed_handler.FeedPollRepository") as repository:
        repository.return_value.load_target.side_effect = FeedPollDisabled()
        assert PollFeedHandler(_engine(), fetcher)(_lease()) is None
        fetcher.fetch.assert_not_called()
