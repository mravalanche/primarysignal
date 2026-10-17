"""A new local processor process reclaims one synthetic feed job safely."""

import base64
import json
import os
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
from sqlalchemy import create_engine, insert, select, text, update

from primary_signal.identity.urls import identify_url
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.repository import LostLease
from primary_signal.jobs.transactions import TransactionalJobQueue
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
