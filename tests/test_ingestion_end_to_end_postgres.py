"""Synthetic feeds through scheduling, retrieval, persistence, and inventory."""

import asyncio
import hashlib
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, func, insert, select, text, update

from primary_signal.config import ProcessorSettings
from primary_signal.entrypoints.processor import ProcessorRuntime, build_feed_poll_handlers
from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.article_handler import ArticleRetrievalHandler
from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.ingestion.inventory import ArticleInventoryRepository, ArticleListQuery
from primary_signal.ingestion.models import (
    Article,
    ArticleUrl,
    ContentVersion,
    FeedEntry,
    FeedPollRun,
    FetchAttempt,
)
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import RetrieveArticleV1
from primary_signal.jobs.handlers import JobProcessingError
from primary_signal.jobs.models import Job
from primary_signal.jobs.transactions import TransactionalJobQueue
from primary_signal.retrieval.api import create_retriever_app
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.client import RetrieverClient
from primary_signal.retrieval.extract import extract_article_html
from primary_signal.sources.models import Feed, Source
from primary_signal.sources.scheduling import TransactionalFeedScheduler


def _bridge(app: FastAPI, request: httpx.Request) -> httpx.Response:
    async def dispatch() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://retriever.example.test",
        ) as client:
            response = await client.request(
                request.method, request.url.path, headers=request.headers, content=request.read()
            )
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx.ByteStream(await response.aread()),
            )

    return asyncio.run(dispatch())


@pytest.mark.postgres
def test_three_synthetic_source_classes_reach_search_with_retry_and_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not database_url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")

    engine = create_engine(database_url, hide_parameters=True)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", database_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    marker = uuid.uuid7().hex
    worker_id = f"test:{uuid.UUID(marker)}"
    classes = ("advisory", "research", "journal")
    source_ids = {kind: uuid.uuid7() for kind in classes}
    feed_ids = {kind: uuid.uuid7() for kind in classes}
    feed_urls = {kind: f"https://{kind}.public.example/{marker}/feed" for kind in classes}
    article_urls = {kind: f"https://{kind}.public.example/{marker}/notice" for kind in classes}
    # RSS, Atom, and RDF/RSS 1.0 exercise different publisher feed shapes.
    feed_bodies = {
        "advisory": (
            f"<rss><channel><item><guid>advisory-{marker}</guid><link>{article_urls['advisory']}</link>"
            "<title>Advisory notice</title></item></channel></rss>"
        ).encode(),
        "research": (
            f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>research-{marker}</id>'
            f'<link href="{article_urls["research"]}"/><title>Research note</title></entry></feed>'
        ).encode(),
        "journal": (
            f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            f'xmlns="http://purl.org/rss/1.0/"><item><link>{article_urls["journal"]}</link>'
            "<title>Journal analysis</title></item></rdf:RDF>"
        ).encode(),
    }
    article_bodies = {
        kind: f"<html><title>{kind.title()} notice</title><article>{kind} cobalt bulletin text.</article></html>".encode()
        for kind in classes
    }
    feed_calls: list[tuple[str, str | None]] = []
    article_calls: list[str] = []
    fail_once = True

    def feed_fetch(url: str, etag: str | None, _modified: str | None) -> FeedFetchResult:
        feed_calls.append((url, etag))
        kind = next(kind for kind in classes if url == feed_urls[kind])
        return FeedFetchResult(200, url, feed_bodies[kind], f'"{kind}-v1"', None)

    def article_fetch(url: str, _etag: str | None, _modified: str | None) -> ArticleFetchResult:
        nonlocal fail_once
        article_calls.append(url)
        if url == article_urls["research"] and fail_once:
            fail_once = False
            raise FeedFetchError("fetch_timeout")
        kind = next(kind for kind in classes if url == article_urls[kind])
        body = article_bodies[kind]
        return ArticleFetchResult(
            200,
            url,
            (),
            "text/html; charset=utf-8",
            len(body),
            hashlib.sha256(body).hexdigest(),
            extract_article_html(body, "text/html; charset=utf-8"),
            None,
            None,
        )

    app = create_retriever_app(fetch=feed_fetch, fetch_article=article_fetch)
    catalogue = build_default_catalogue()
    scheduler = TransactionalFeedScheduler(engine, catalogue)
    queue = TransactionalJobQueue(engine, catalogue)
    previous_poll_times: list[tuple[uuid.UUID, datetime | None]] = []

    def promote(job_type: str, keys: set[str]) -> None:
        with engine.begin() as connection:
            connection.execute(
                update(Job)
                .where(
                    Job.job_type == job_type,
                    Job.deduplication_key.in_(keys),
                    Job.status == "queued",
                )
                .values(priority=100, run_after=datetime(1970, 1, 1, tzinfo=UTC))
            )

    try:
        with engine.begin() as connection:
            previous_poll_times = [
                (feed_id, next_poll_at)
                for feed_id, next_poll_at in connection.execute(select(Feed.id, Feed.next_poll_at))
            ]
            connection.execute(update(Feed).values(next_poll_at=None))
            for kind in classes:
                feed_identity = identify_url(feed_urls[kind])
                connection.execute(
                    insert(Source).values(
                        id=source_ids[kind],
                        source_key=f"e2e-{kind}-{marker}",
                        name=f"Synthetic {kind}",
                        homepage_url=f"https://{kind}.public.example/",
                    )
                )
                connection.execute(
                    insert(Feed).values(
                        id=feed_ids[kind],
                        source_id=source_ids[kind],
                        name=f"Synthetic {kind} feed",
                        configured_url=feed_urls[kind],
                        normalized_url=feed_identity.normalized_url,
                        url_hash=feed_identity.url_hash,
                        url_normalization_version=feed_identity.normalization_version,
                        next_poll_at=datetime.now(UTC) - timedelta(minutes=1),
                    )
                )

        with RetrieverClient(
            "http://retriever.example.test",
            transport=httpx.MockTransport(lambda request: _bridge(app, request)),
        ) as retriever:
            feed_runtime = ProcessorRuntime(
                queue,
                build_feed_poll_handlers(engine, retriever),
                ProcessorSettings(),
                threading.Event(),
            )
            assert scheduler.schedule_due(limit=3).enqueued == 3
            promote("feeds.poll", {f"feed:{feed_id}" for feed_id in feed_ids.values()})
            for _ in classes:
                feed_runtime.run(once=True)

            with engine.connect() as connection:
                poll_jobs = connection.execute(
                    select(Job.deduplication_key, Job.status, Job.last_error_code).where(
                        Job.job_type == "feeds.poll",
                        Job.deduplication_key.in_(
                            {f"feed:{feed_id}" for feed_id in feed_ids.values()}
                        ),
                    )
                ).all()
                assert all(row.status == "succeeded" for row in poll_jobs), poll_jobs
                for kind in classes:
                    entry = connection.execute(
                        select(
                            FeedEntry.article_id, FeedEntry.identity_method, FeedEntry.reported_url
                        ).where(FeedEntry.feed_id == feed_ids[kind])
                    ).one()
                    assert entry.article_id is not None
                    assert entry.reported_url == article_urls[kind]
                    assert entry.identity_method == ("url" if kind == "journal" else "guid")
                    assert (
                        connection.execute(
                            select(ArticleUrl.normalized_url).where(
                                ArticleUrl.article_id == entry.article_id
                            )
                        ).scalar_one()
                        == article_urls[kind]
                    )
                    assert connection.execute(
                        select(FeedPollRun.status, FeedPollRun.entries_discovered).where(
                            FeedPollRun.feed_id == feed_ids[kind]
                        )
                    ).one() == ("succeeded", 1)

            article_ids: dict[str, uuid.UUID] = {}
            with engine.connect() as connection:
                for kind in classes:
                    article_ids[kind] = connection.execute(
                        select(Article.id).where(Article.source_id == source_ids[kind])
                    ).scalar_one()
            promote(
                "articles.retrieve",
                {f"article:{article_id}" for article_id in article_ids.values()},
            )
            article_handler = ArticleRetrievalHandler(engine, retriever)
            outcomes: list[str] = []
            for _ in classes:
                lease = queue.claim(queue="retrieval", worker_id=worker_id)
                assert lease is not None
                assert isinstance(lease.payload, RetrieveArticleV1)
                assert lease.payload.article_id in article_ids.values()
                try:
                    callback = article_handler(lease)
                except JobProcessingError as failure:
                    outcomes.append(failure.failure.code)
                    queue.fail(lease, failure.failure, on_failure=failure.on_failure)
                else:
                    outcomes.append("succeeded")
                    queue.succeed(lease, on_success=callback)
            assert sorted(outcomes) == ["dependency_timeout", "succeeded", "succeeded"]

            with engine.connect() as connection:
                failed_article = article_ids["research"]
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FetchAttempt)
                        .where(
                            FetchAttempt.article_id == failed_article,
                            FetchAttempt.status == "failed",
                            FetchAttempt.error_code == "dependency_timeout",
                        )
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(ContentVersion)
                        .where(ContentVersion.article_id == failed_article)
                    ).scalar_one()
                    == 0
                )
                retry_job = connection.execute(
                    select(Job.id, Job.status)
                    .where(
                        Job.job_type == "articles.retrieve",
                        Job.deduplication_key == f"article:{failed_article}",
                    )
                    .order_by(Job.created_at.desc())
                    .limit(1)
                ).one()
                assert retry_job.status == "queued"
            with engine.begin() as connection:
                connection.execute(
                    update(Job)
                    .where(Job.id == retry_job.id)
                    .values(priority=100, run_after=datetime(1970, 1, 1, tzinfo=UTC))
                )
            lease = queue.claim(queue="retrieval", worker_id=worker_id)
            assert lease is not None
            assert isinstance(lease.payload, RetrieveArticleV1)
            assert lease.payload.article_id == failed_article
            queue.succeed(lease, on_success=article_handler(lease))

            with engine.connect() as connection:
                inventory = ArticleInventoryRepository(connection)
                for kind in classes:
                    article_id = article_ids[kind]
                    result = inventory.list_articles(
                        ArticleListQuery(source_key=f"e2e-{kind}-{marker}", search="cobalt")
                    )
                    assert len(result.items) == 1
                    assert result.items[0].article_id == article_id
                    assert result.items[0].canonical_url == article_urls[kind]
                    version = connection.execute(
                        select(ContentVersion.extracted_text).where(
                            ContentVersion.id
                            == select(Article.current_content_version_id)
                            .where(Article.id == article_id)
                            .scalar_subquery()
                        )
                    ).scalar_one()
                    assert version == f"{kind} cobalt bulletin text."
                    assert (
                        connection.execute(
                            select(func.count())
                            .select_from(FetchAttempt)
                            .where(
                                FetchAttempt.article_id == article_id,
                                FetchAttempt.status == "fetched",
                            )
                        ).scalar_one()
                        == 1
                    )

            # Polling identical content retains one entry, one article, and one version.
            with engine.begin() as connection:
                connection.execute(
                    update(Feed)
                    .where(Feed.id == feed_ids["advisory"])
                    .values(next_poll_at=datetime.now(UTC) - timedelta(minutes=1))
                )
            assert scheduler.schedule_due(limit=1).enqueued == 1
            promote("feeds.poll", {f"feed:{feed_ids['advisory']}"})
            feed_runtime.run(once=True)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FeedPollRun)
                        .where(
                            FeedPollRun.feed_id == feed_ids["advisory"],
                            FeedPollRun.status == "succeeded",
                        )
                    ).scalar_one()
                    == 2
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FeedEntry)
                        .where(FeedEntry.feed_id == feed_ids["advisory"])
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(Article)
                        .where(Article.source_id == source_ids["advisory"])
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(ContentVersion)
                        .where(ContentVersion.article_id == article_ids["advisory"])
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(Job)
                        .where(
                            Job.job_type == "articles.retrieve",
                            Job.deduplication_key == f"article:{article_ids['advisory']}",
                        )
                    ).scalar_one()
                    == 1
                )
        assert len(feed_calls) == 4
        assert (feed_urls["advisory"], '"advisory-v1"') in feed_calls
        assert article_calls.count(article_urls["research"]) == 2
    finally:
        with engine.begin() as connection:
            connection.execute(
                update(Feed)
                .where(Feed.id.in_(feed_ids.values()))
                .values(enabled=False, next_poll_at=None)
            )
            for previous_id, next_poll_at in previous_poll_times:
                connection.execute(
                    update(Feed).where(Feed.id == previous_id).values(next_poll_at=next_poll_at)
                )
        engine.dispose()
