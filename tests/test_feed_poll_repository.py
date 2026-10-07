"""Feed-poll transaction decisions with prepared, synthetic observations."""

import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection

from primary_signal.ingestion.feed_parser import parse_feed
from primary_signal.ingestion.feed_polls import (
    FeedFetchResult,
    FeedPollDisabled,
    FeedPollRepository,
)
from primary_signal.jobs.repository import EnqueueResult, JobRepository

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
FEED_URL = "https://public.example/feed.xml"


class _Result:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


def _feed(*, enabled: bool = True, url: str = FEED_URL) -> dict[str, object]:
    return {
        "id": uuid.uuid7(),
        "source_id": uuid.uuid7(),
        "configured_url": url,
        "normalized_url": url,
        "etag": '"previous"',
        "last_modified": None,
        "consecutive_failures": 1,
        "enabled": enabled,
    }


def _connection(*results: object) -> MagicMock:
    connection = MagicMock(spec=Connection)
    connection.execute.side_effect = [_Result(result) for result in results]
    return connection


def _queue(*, created: bool = True) -> MagicMock:
    queue = MagicMock(spec=JobRepository)
    queue.enqueue.return_value = EnqueueResult(job_id=uuid.uuid7(), created=created)
    return queue


def test_poll_target_requires_an_enabled_feed_and_source() -> None:
    feed_id = uuid.uuid7()
    enabled = _connection(
        {
            "normalized_url": FEED_URL,
            "etag": '"previous"',
            "last_modified": None,
            "enabled": True,
            "source_enabled": True,
        }
    )
    target = FeedPollRepository(cast(Connection, enabled)).load_target(feed_id)
    assert target.feed_id == feed_id
    assert target.url == FEED_URL
    assert target.etag == '"previous"'

    for unavailable in (
        None,
        {"enabled": False, "source_enabled": True},
        {"enabled": True, "source_enabled": False},
    ):
        connection = _connection(unavailable)
        with pytest.raises(FeedPollDisabled):
            FeedPollRepository(cast(Connection, connection)).load_target(feed_id)


def test_new_feed_entry_creates_an_article_and_one_retrieval_job() -> None:
    feed = _feed()
    entry_id = uuid.uuid7()
    url_id = uuid.uuid7()
    connection = _connection(
        feed,
        True,
        None,
        entry_id,
        None,
        None,
        None,
        url_id,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    queue = _queue()
    entries = parse_feed(
        b"<rss><channel><item><guid>notice-1</guid>"
        b"<link>https://public.example/notice-1</link></item></channel></rss>"
    )

    summary = FeedPollRepository(cast(Connection, connection)).record_result(
        feed_id=cast(uuid.UUID, feed["id"]),
        job_id=uuid.uuid7(),
        expected_url=FEED_URL,
        result=FeedFetchResult(status=200, final_url=FEED_URL, body=b"<rss/>", etag='"new"'),
        entries=entries,
        now=NOW,
        queue=cast(JobRepository, queue),
    )

    assert (summary.seen, summary.discovered, summary.retrieval_jobs) == (1, 1, 1)
    queue.enqueue.assert_called_once()
    assert queue.enqueue.call_args.kwargs["job_type"] == "articles.retrieve"
    assert connection.execute.call_count == 14


def test_repoll_of_existing_entry_refreshes_last_seen_without_new_job() -> None:
    feed = _feed()
    article_id = uuid.uuid7()
    connection = _connection(feed, True, None, None, article_id, None, None, None)
    queue = _queue()
    entries = parse_feed(
        b"<rss><channel><item><guid>notice-1</guid>"
        b"<link>https://public.example/notice-1</link></item></channel></rss>"
    )

    summary = FeedPollRepository(cast(Connection, connection)).record_result(
        feed_id=cast(uuid.UUID, feed["id"]),
        job_id=uuid.uuid7(),
        expected_url=FEED_URL,
        result=FeedFetchResult(status=200, final_url=FEED_URL, body=b"<rss/>"),
        entries=entries,
        now=NOW,
        queue=cast(JobRepository, queue),
    )

    assert (summary.seen, summary.discovered, summary.retrieval_jobs) == (1, 0, 0)
    queue.enqueue.assert_not_called()
    assert connection.execute.call_count == 8


def test_not_modified_and_disabled_outcomes_do_not_discover_entries() -> None:
    feed = _feed()
    queue = _queue()
    not_modified = _connection(feed, True, None, None, None)
    summary = FeedPollRepository(cast(Connection, not_modified)).record_result(
        feed_id=cast(uuid.UUID, feed["id"]),
        job_id=uuid.uuid7(),
        expected_url=FEED_URL,
        result=FeedFetchResult(status=304, final_url=FEED_URL, body=b""),
        entries=(),
        now=NOW,
        queue=cast(JobRepository, queue),
    )
    assert (summary.seen, summary.discovered, summary.retrieval_jobs) == (0, 0, 0)
    assert not_modified.execute.call_count == 5

    changed = _connection(_feed(url="https://public.example/new-feed"), True)
    stale = FeedPollRepository(cast(Connection, changed)).record_result(
        feed_id=cast(uuid.UUID, feed["id"]),
        job_id=uuid.uuid7(),
        expected_url=FEED_URL,
        result=FeedFetchResult(status=304, final_url=FEED_URL, body=b""),
        entries=(),
        now=NOW,
        queue=cast(JobRepository, queue),
    )
    assert stale.discovered == 0
    assert changed.execute.call_count == 2
    queue.enqueue.assert_not_called()


def test_failed_poll_records_only_current_enabled_target() -> None:
    feed = _feed()
    connection = _connection(feed, True, None, None)
    FeedPollRepository(cast(Connection, connection)).record_failure(
        feed_id=cast(uuid.UUID, feed["id"]),
        job_id=uuid.uuid7(),
        expected_url=FEED_URL,
        error_code="dependency_timeout",
        now=NOW,
    )
    assert connection.execute.call_count == 4

    stale = _connection(_feed(enabled=False))
    FeedPollRepository(cast(Connection, stale)).record_failure(
        feed_id=cast(uuid.UUID, feed["id"]),
        job_id=uuid.uuid7(),
        expected_url=FEED_URL,
        error_code="dependency_timeout",
        now=NOW,
    )
    assert stale.execute.call_count == 1
