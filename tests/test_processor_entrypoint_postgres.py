"""Local processor processes recover abandoned synthetic feed jobs safely."""

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Text, create_engine, func, insert, select, text, update

from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.inventory import ArticleInventoryRepository, ArticleListQuery
from primary_signal.ingestion.models import (
    Article,
    ContentVersion,
    FeedEntry,
    FeedPollRun,
    FetchAttempt,
)
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import PollFeedV1, RetrieveArticleV1
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.repository import JobLease, LostLease
from primary_signal.jobs.transactions import TransactionalJobQueue
from primary_signal.retrieval.extract import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    NORMALIZATION_VERSION,
    extract_article_html,
)
from primary_signal.sources.models import Feed, Source


@pytest.mark.postgres
def test_new_process_reclaims_expired_lease_and_fences_old_worker(
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
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", database_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    marker = uuid.uuid7().hex
    source_id = uuid.uuid7()
    feed_id = uuid.uuid7()
    feed_url = f"https://advisory.public.example/{marker}/feed"
    identity = identify_url(feed_url)
    catalogue = build_default_catalogue()
    queue = TransactionalJobQueue(engine, catalogue)
    requests: list[str] = []

    class SyntheticRetriever(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = b"<rss><channel></channel></rss>"
            payload = {
                "status": 200,
                "final_url": feed_url,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "etag": None,
                "last_modified": None,
            }
            response = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *_args: object) -> None:
            _ = format
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticRetriever)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def run_process() -> None:
        completed = subprocess.run(  # noqa: S603 - fixed local Python module and test args.
            [
                sys.executable,
                "-m",
                "primary_signal.entrypoints.processor",
                "--once",
                "--queue",
                "ingestion",
                "--retriever-origin",
                f"http://127.0.0.1:{server.server_port}",
                "--allow-local-fetch",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    try:
        with engine.begin() as connection:
            connection.execute(
                insert(Source).values(
                    id=source_id,
                    source_key=f"restart-{marker}",
                    name="Synthetic advisory",
                    homepage_url="https://advisory.public.example/",
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
                )
            )
        job_id = queue.enqueue(
            job_type="feeds.poll", payload_version=1, payload=PollFeedV1(feed_id=feed_id)
        ).job_id
        abandoned = queue.claim(
            queue="ingestion", worker_id=f"abandoned:{uuid.uuid4()}", lease_seconds=5
        )
        assert abandoned is not None
        assert abandoned.job_id == job_id
        with engine.begin() as connection:
            connection.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

        run_process()
        with pytest.raises(LostLease):
            queue.succeed(abandoned)
        with engine.begin() as connection:
            assert connection.execute(select(Job.status).where(Job.id == job_id)).scalar_one() == (
                "queued"
            )
            connection.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(run_after=datetime(1970, 1, 1, tzinfo=UTC))
            )
        run_process()
        with engine.connect() as connection:
            assert connection.execute(select(Job.status).where(Job.id == job_id)).scalar_one() == (
                "succeeded"
            )
            assert connection.execute(
                select(JobAttempt.status)
                .where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.attempt_number)
            ).scalars().all() == ["lease_expired", "succeeded"]
        assert requests == ["/v1/feeds/fetch"]
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        with engine.begin() as connection:
            connection.execute(update(Feed).where(Feed.id == feed_id).values(enabled=False))
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX SIGKILL semantics")
@pytest.mark.parametrize("queue_name", ["ingestion", "retrieval"])
def test_killed_processor_recovers_without_partial_or_duplicate_effects(
    monkeypatch: pytest.MonkeyPatch,
    queue_name: str,
) -> None:
    database_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not database_url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")
    if os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true":
        pytest.skip("requires an explicitly disposable PostgreSQL database")

    engine = create_engine(database_url, hide_parameters=True)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", database_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    marker = uuid.uuid7().hex
    source_id, feed_id = uuid.uuid7(), uuid.uuid7()
    feed_url = f"https://advisory.public.example/{marker}/feed"
    article_url = f"https://advisory.public.example/{marker}/notice"
    identity = identify_url(feed_url)
    body = (
        "<rss><channel><item>"
        f"<guid>notice-{marker}</guid><link>{article_url}</link>"
        "<title>Synthetic advisory notice</title></item></channel></rss>"
    ).encode()
    article_body = (
        b"<html><title>Synthetic advisory notice</title>"
        b"<article>Cobalt advisory recovery bulletin.</article></html>"
    )
    extraction = extract_article_html(article_body, "text/html")
    article_payload: dict[str, object] = {
        "status": 200,
        "final_url": article_url,
        "redirect_chain": [],
        "content_type": "text/html",
        "decoded_byte_count": len(article_body),
        "raw_response_hash": hashlib.sha256(article_body).hexdigest(),
        "extracted_title": extraction.title,
        "extracted_text": extraction.text,
        "word_count": extraction.word_count,
        "normalized_content_hash": extraction.normalized_content_hash,
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "etag": None,
        "last_modified": None,
    }
    queue = TransactionalJobQueue(engine, build_default_catalogue())
    request_started = threading.Event()
    release_request = threading.Event()
    requests: list[str] = []
    blocked_path = "/v1/feeds/fetch" if queue_name == "ingestion" else "/v1/articles/fetch"

    class BlockingRetriever(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            requests.append(self.path)
            if self.path == blocked_path and not request_started.is_set():
                # The worker has committed its claim but cannot prepare a
                # completion callback until this response is released.
                request_started.set()
                release_request.wait(timeout=20)
                return
            if self.path == "/v1/feeds/fetch":
                payload: dict[str, object] = {
                    "status": 200,
                    "final_url": feed_url,
                    "body_base64": base64.b64encode(body).decode("ascii"),
                    "etag": None,
                    "last_modified": None,
                }
            elif self.path == "/v1/articles/fetch":
                payload = article_payload
            else:
                self.send_error(404)
                return
            response = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *_args: object) -> None:
            _ = format

    server = ThreadingHTTPServer(("127.0.0.1", 0), BlockingRetriever)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    process: subprocess.Popen[str] | None = None
    job_id: uuid.UUID | None = None

    def processor_args(selected_queue: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "primary_signal.entrypoints.processor",
            "--once",
            "--queue",
            selected_queue,
            "--retriever-origin",
            f"http://127.0.0.1:{server.server_port}",
            "--allow-local-fetch",
        ]

    def run_replacement(selected_queue: str = queue_name) -> None:
        completed = subprocess.run(  # noqa: S603 - fixed local module and synthetic args.
            processor_args(selected_queue),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    def assert_no_observations() -> None:
        existing_lineage = 0 if queue_name == "ingestion" else 1
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(FeedPollRun)
                    .where(FeedPollRun.feed_id == feed_id)
                ).scalar_one()
                == existing_lineage
            )
            assert (
                connection.execute(
                    select(func.count()).select_from(FeedEntry).where(FeedEntry.feed_id == feed_id)
                ).scalar_one()
                == existing_lineage
            )
            assert (
                connection.execute(
                    select(func.count()).select_from(Article).where(Article.source_id == source_id)
                ).scalar_one()
                == existing_lineage
            )
            article_ids = select(Article.id).where(Article.source_id == source_id)
            for model in (ContentVersion, FetchAttempt):
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(model)
                        .where(model.article_id.in_(article_ids))
                    ).scalar_one()
                    == 0
                )
            assert (
                connection.execute(
                    select(Article.current_content_version_id).where(Article.source_id == source_id)
                )
                .scalars()
                .all()
                == [None] * existing_lineage
            )
            assert (
                not ArticleInventoryRepository(connection)
                .list_articles(ArticleListQuery(source_key=f"killed-worker-{marker}"))
                .items
            )

    try:
        with engine.begin() as connection:
            connection.execute(
                insert(Source).values(
                    id=source_id,
                    source_key=f"killed-worker-{marker}",
                    name="Synthetic advisory",
                    homepage_url="https://advisory.public.example/",
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
                )
            )
        job_id = queue.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=feed_id),
            priority=100,
            run_after=datetime(1970, 1, 1, tzinfo=UTC),
        ).job_id
        payload: PollFeedV1 | RetrieveArticleV1 = PollFeedV1(feed_id=feed_id)
        job_type = "feeds.poll"
        if queue_name == "retrieval":
            # Discover the article through the real feed handler rather than
            # fabricating its URL and article identity relationships.
            run_replacement("ingestion")
            with engine.begin() as connection:
                article = connection.execute(
                    select(Article.id, Article.current_canonical_url_id).where(
                        Article.source_id == source_id
                    )
                ).one()
                assert article.current_canonical_url_id is not None
                payload = RetrieveArticleV1(
                    article_id=article.id, article_url_id=article.current_canonical_url_id
                )
                job_type = "articles.retrieve"
                job_id = connection.execute(
                    select(Job.id).where(
                        Job.job_type == job_type,
                        Job.payload["article_id"].astext == str(article.id),
                    )
                ).scalar_one()
                connection.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(priority=100, run_after=datetime(1970, 1, 1, tzinfo=UTC))
                )
        process = subprocess.Popen(  # noqa: S603 - fixed local module and synthetic args.
            processor_args(queue_name), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert request_started.wait(timeout=10), "processor never reached the synthetic retriever"
        assert process.poll() is None
        with engine.connect() as connection:
            running = connection.execute(
                select(
                    Job.status,
                    Job.worker_id,
                    Job.lease_token,
                    Job.lease_expires_at,
                    Job.attempt_count,
                    JobAttempt.id,
                )
                .join(JobAttempt, JobAttempt.job_id == Job.id)
                .where(Job.id == job_id, JobAttempt.attempt_number == 1)
            ).one()
        assert running.status == "running"
        assert running.worker_id is not None
        assert running.lease_token is not None
        assert running.lease_expires_at is not None
        abandoned = JobLease(
            job_id=job_id,
            attempt_id=running.id,
            attempt_number=running.attempt_count,
            lease_token=running.lease_token,
            worker_id=running.worker_id,
            lease_expires_at=running.lease_expires_at,
            job_type=job_type,
            payload_version=1,
            payload=payload,
        )
        assert_no_observations()
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL
        release_request.set()
        assert_no_observations()
        with engine.begin() as connection:
            assert connection.execute(select(Job.status).where(Job.id == job_id)).scalar_one() == (
                "running"
            )
            # Advance only the owned lease rather than sleeping through a
            # runtime lease and randomized retry interval.
            connection.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

        # Recovery covers all queues. Use the drained feed queue for this
        # pass so delayed article retries cannot consume unrelated retrieval
        # work left by other integration tests before the owned retry is due.
        run_replacement("ingestion")
        with engine.begin() as connection:
            assert connection.execute(select(Job.status).where(Job.id == job_id)).scalar_one() == (
                "queued"
            )
            connection.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(run_after=datetime(1970, 1, 1, tzinfo=UTC))
            )
        run_replacement()

        with pytest.raises(LostLease):
            queue.succeed(abandoned)
        version_id: uuid.UUID | None = None
        with engine.connect() as connection:
            assert connection.execute(select(Job.status).where(Job.id == job_id)).scalar_one() == (
                "succeeded"
            )
            assert connection.execute(
                select(JobAttempt.status)
                .where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.attempt_number)
            ).scalars().all() == ["lease_expired", "succeeded"]
            assert connection.execute(
                select(FeedPollRun.status).where(FeedPollRun.feed_id == feed_id)
            ).scalars().all() == ["succeeded"]
            article_ids = (
                connection.execute(select(Article.id).where(Article.source_id == source_id))
                .scalars()
                .all()
            )
            assert len(article_ids) == 1
            assert (
                connection.execute(select(FeedEntry.article_id).where(FeedEntry.feed_id == feed_id))
                .scalars()
                .all()
                == article_ids
            )
            assert connection.execute(
                select(Job.status).where(
                    Job.job_type == "articles.retrieve",
                    Job.payload["article_id"].astext == str(article_ids[0]),
                )
            ).scalars().all() == ["queued" if queue_name == "ingestion" else "succeeded"]
            if queue_name == "retrieval":
                version_id = connection.execute(
                    select(ContentVersion.id).where(ContentVersion.article_id == article_ids[0])
                ).scalar_one()
                assert (
                    connection.execute(
                        select(Article.current_content_version_id).where(
                            Article.id == article_ids[0]
                        )
                    ).scalar_one()
                    == version_id
                )
                assert connection.execute(
                    select(FetchAttempt.status).where(FetchAttempt.article_id == article_ids[0])
                ).scalars().all() == ["fetched"]
                inventory = ArticleInventoryRepository(connection).list_articles(
                    ArticleListQuery(search="cobalt", source_key=f"killed-worker-{marker}")
                )
                assert [item.article_id for item in inventory.items] == article_ids
                assert inventory.items[0].title == extraction.title

        if queue_name == "retrieval":
            assert version_id is not None
            # Re-fetching unchanged content must retain its immutable version
            # while recording a distinct successful observation.
            repeated_job_id = queue.enqueue(
                job_type=job_type,
                payload_version=1,
                payload=payload,
                priority=100,
                run_after=datetime(1970, 1, 1, tzinfo=UTC),
            ).job_id
            run_replacement()
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        select(Job.status).where(Job.id == repeated_job_id)
                    ).scalar_one()
                    == "succeeded"
                )
                assert connection.execute(
                    select(ContentVersion.id).where(ContentVersion.article_id == article_ids[0])
                ).scalars().all() == [version_id]
                assert connection.execute(
                    select(FetchAttempt.status).where(FetchAttempt.article_id == article_ids[0])
                ).scalars().all() == ["fetched", "fetched"]
            assert requests == [
                "/v1/feeds/fetch",
                "/v1/articles/fetch",
                "/v1/articles/fetch",
                "/v1/articles/fetch",
            ]
        else:
            assert requests == ["/v1/feeds/fetch", "/v1/feeds/fetch"]
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        release_request.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        with engine.begin() as connection:
            connection.execute(update(Feed).where(Feed.id == feed_id).values(enabled=False))
            connection.execute(update(Source).where(Source.id == source_id).values(enabled=False))
            # Prevent this test's successor from being claimed by another
            # integration test using the same disposable cluster.
            successor_ids = (
                connection.execute(
                    select(Job.id).where(
                        Job.status == "queued",
                        Job.job_type == "articles.retrieve",
                        Job.payload["article_id"].astext.in_(
                            select(Article.id.cast(Text)).where(Article.source_id == source_id)
                        ),
                    )
                )
                .scalars()
                .all()
            )
        for successor_id in successor_ids:
            queue.cancel_queued(successor_id)
        if job_id is not None:
            queue.cancel_queued(job_id)
        engine.dispose()
