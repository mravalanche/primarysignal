"""PostgreSQL proof that repeated polls keep one entry and retrieval job."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, text

from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.feed_parser import parse_feed
from primary_signal.ingestion.feed_polls import FeedFetchResult, FeedPollRepository
from primary_signal.ingestion.models import Article, ArticleUrl, FeedEntry, FeedPollRun
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.models import Job
from primary_signal.jobs.repository import JobRepository
from primary_signal.sources.models import Feed, Source


@pytest.mark.postgres
def test_repeated_feed_poll_is_idempotent_in_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")

    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    feed_id = uuid.uuid7()
    source_id = uuid.uuid7()
    feed_url = f"https://public.example/feed/{feed_id}.xml"
    identity = identify_url(feed_url)
    body = (
        b"<rss><channel><item><guid>notice-1</guid>"
        b"<link>https://public.example/notice-1</link>"
        b"<title>Example notice</title></item></channel></rss>"
    )
    now = datetime.now(UTC)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    insert(Source).values(
                        id=source_id,
                        source_key=f"poll-{uuid.uuid4().hex}",
                        name="Public Example",
                        homepage_url="https://public.example/",
                    )
                )
                connection.execute(
                    insert(Feed).values(
                        id=feed_id,
                        source_id=source_id,
                        name="Example feed",
                        configured_url=feed_url,
                        normalized_url=identity.normalized_url,
                        url_hash=identity.url_hash,
                        url_normalization_version=identity.normalization_version,
                        next_poll_at=now,
                    )
                )
                queue = JobRepository(connection, build_default_catalogue())
                poller = FeedPollRepository(connection)
                first_job = queue.enqueue(
                    job_type="feeds.poll", payload_version=1, payload=PollFeedV1(feed_id=feed_id)
                )
                first = poller.record_result(
                    feed_id=feed_id,
                    job_id=first_job.job_id,
                    expected_url=identity.normalized_url,
                    result=FeedFetchResult(
                        status=200, final_url=feed_url, body=body, etag='"first"'
                    ),
                    entries=parse_feed(body),
                    now=now,
                    queue=queue,
                )
                second_job = queue.enqueue(
                    job_type="feeds.poll", payload_version=1, payload=PollFeedV1(feed_id=feed_id)
                )
                second = poller.record_result(
                    feed_id=feed_id,
                    job_id=second_job.job_id,
                    expected_url=identity.normalized_url,
                    result=FeedFetchResult(status=200, final_url=feed_url, body=body),
                    entries=parse_feed(body),
                    now=now + timedelta(minutes=1),
                    queue=queue,
                )
                third_job = queue.enqueue(
                    job_type="feeds.poll", payload_version=1, payload=PollFeedV1(feed_id=feed_id)
                )
                third = poller.record_result(
                    feed_id=feed_id,
                    job_id=third_job.job_id,
                    expected_url=identity.normalized_url,
                    result=FeedFetchResult(status=304, final_url=feed_url, body=b""),
                    entries=(),
                    now=now + timedelta(minutes=2),
                    queue=queue,
                )

                assert (first.discovered, first.retrieval_jobs) == (1, 1)
                assert (second.discovered, second.retrieval_jobs) == (0, 0)
                assert (third.seen, third.discovered, third.retrieval_jobs) == (0, 0, 0)
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FeedEntry)
                        .where(FeedEntry.feed_id == feed_id)
                    ).scalar_one()
                    == 1
                )
                article_id = connection.execute(
                    select(Article.id).where(Article.source_id == source_id)
                ).scalar_one()
                assert connection.execute(
                    select(Article.last_seen_at).where(Article.id == article_id)
                ).scalar_one() == now + timedelta(minutes=1)
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(ArticleUrl)
                        .join(Article, Article.id == ArticleUrl.article_id)
                        .where(Article.source_id == source_id)
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(Job)
                        .where(
                            Job.job_type == "articles.retrieve",
                            Job.deduplication_key == f"article:{article_id}",
                        )
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FeedPollRun)
                        .where(FeedPollRun.feed_id == feed_id)
                    ).scalar_one()
                    == 3
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
