"""Synthetic PostgreSQL proof of the complete feed scheduling path."""

import asyncio
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, insert, select, text, update

from primary_signal.config import ProcessorSettings
from primary_signal.entrypoints.processor import ProcessorRuntime, build_feed_poll_handlers
from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.ingestion.models import Article, ArticleUrl, FeedEntry, FeedPollRun
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.transactions import TransactionalJobQueue
from primary_signal.retrieval.api import create_retriever_app
from primary_signal.retrieval.client import RetrieverClient
from primary_signal.sources.models import Feed, Source
from primary_signal.sources.scheduling import TransactionalFeedScheduler


@pytest.mark.postgres
def test_scheduler_processor_retriever_pipeline_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")

    engine: Engine = create_engine(url, hide_parameters=True)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    source_id, feed_id = uuid.uuid7(), uuid.uuid7()
    feed_url = f"https://feed.public.example/{feed_id}.xml"
    article_url = f"https://feed.public.example/notice/{feed_id}"
    identity = identify_url(feed_url)
    body = (
        b"<rss><channel><item><guid>notice-1</guid><link>"
        + article_url.encode("ascii")
        + b"</link><title>Example notice</title></item></channel></rss>"
    )
    calls: list[tuple[str, str | None, str | None]] = []

    def fake_fetch(
        requested_url: str, etag: str | None, last_modified: str | None
    ) -> FeedFetchResult:
        calls.append((requested_url, etag, last_modified))
        return FeedFetchResult(200, feed_url, body, '"synthetic"', None)

    app = create_retriever_app(fetch=fake_fetch)

    def bridge(request: httpx.Request) -> httpx.Response:
        async def dispatch() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://retriever.example.test",
            ) as client:
                response = await client.request(
                    request.method,
                    request.url.path,
                    headers=request.headers,
                    content=request.read(),
                )
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=await response.aread(),
                )

        return asyncio.run(dispatch())

    catalogue = build_default_catalogue()
    scheduler = TransactionalFeedScheduler(engine, catalogue)
    queue = TransactionalJobQueue(engine, catalogue)
    previous_poll_times: list[tuple[uuid.UUID, datetime | None]] = []
    try:
        with engine.begin() as connection:
            previous_poll_times = [
                (feed, poll_time)
                for feed, poll_time in connection.execute(select(Feed.id, Feed.next_poll_at))
            ]
            connection.execute(update(Feed).values(next_poll_at=None))
            connection.execute(
                insert(Source).values(
                    id=source_id,
                    source_key=f"pipeline-test-{source_id.hex}",
                    name="Synthetic source",
                    homepage_url="https://feed.public.example/",
                )
            )
            connection.execute(
                insert(Feed).values(
                    id=feed_id,
                    source_id=source_id,
                    name="Synthetic feed",
                    configured_url=feed_url,
                    normalized_url=identity.normalized_url,
                    url_hash=identity.url_hash,
                    url_normalization_version=identity.normalization_version,
                    next_poll_at=datetime.now(UTC) - timedelta(minutes=1),
                )
            )

        with RetrieverClient(
            "http://retriever.example.test", transport=httpx.MockTransport(bridge)
        ) as retriever:
            runtime = ProcessorRuntime(
                queue,
                build_feed_poll_handlers(engine, retriever),
                ProcessorSettings(),
                threading.Event(),
            )
            for pass_number in (1, 2):
                if pass_number == 2:
                    with engine.begin() as connection:
                        connection.execute(
                            update(Feed)
                            .where(Feed.id == feed_id)
                            .values(next_poll_at=datetime.now(UTC) - timedelta(minutes=1))
                        )
                summary = scheduler.schedule_due(limit=1)
                assert (summary.selected, summary.enqueued) == (1, 1)
                runtime.run(once=True)

                with engine.connect() as connection:
                    runs = connection.execute(
                        select(FeedPollRun.status, FeedPollRun.entries_discovered)
                        .where(FeedPollRun.feed_id == feed_id)
                        .order_by(FeedPollRun.started_at)
                    ).all()
                    assert len(runs) == pass_number
                    assert [status for status, _ in runs] == ["succeeded"] * pass_number
                    assert [discovered for _, discovered in runs] == [1] + [0] * (pass_number - 1)
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
                    assert (
                        connection.execute(
                            select(func.count())
                            .select_from(ArticleUrl)
                            .where(ArticleUrl.article_id == article_id)
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
                                Job.status == "queued",
                            )
                        ).scalar_one()
                        == 1
                    )

        assert calls == [(feed_url, None, None), (feed_url, '"synthetic"', None)]
    finally:
        with engine.begin() as connection:
            article_ids = list(
                connection.execute(
                    select(Article.id).where(Article.source_id == source_id)
                ).scalars()
            )
            job_ids = list(
                connection.execute(
                    select(Job.id).where(
                        Job.deduplication_key.in_(
                            [f"feed:{feed_id}"]
                            + [f"article:{article_id}" for article_id in article_ids]
                        )
                    )
                ).scalars()
            )
            connection.execute(delete(FeedEntry).where(FeedEntry.feed_id == feed_id))
            connection.execute(delete(FeedPollRun).where(FeedPollRun.feed_id == feed_id))
            if article_ids:
                connection.execute(
                    update(Article)
                    .where(Article.id.in_(article_ids))
                    .values(current_canonical_url_id=None)
                )
                connection.execute(delete(ArticleUrl).where(ArticleUrl.article_id.in_(article_ids)))
                connection.execute(delete(Article).where(Article.id.in_(article_ids)))
            if job_ids:
                connection.execute(delete(JobAttempt).where(JobAttempt.job_id.in_(job_ids)))
                connection.execute(delete(Job).where(Job.id.in_(job_ids)))
            connection.execute(delete(Feed).where(Feed.id == feed_id))
            connection.execute(delete(Source).where(Source.id == source_id))
            for previous_id, next_poll_at in previous_poll_times:
                connection.execute(
                    update(Feed).where(Feed.id == previous_id).values(next_poll_at=next_poll_at)
                )
        engine.dispose()
