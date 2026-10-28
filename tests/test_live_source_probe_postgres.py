"""Exercise the private live-proof SQL with synthetic data and no retrieval."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from scripts.smoke_isolated_retrieval import LIVE_DATABASE_PROBE
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url

from primary_signal.ingestion.article_handler import ArticleRetrievalHandler
from primary_signal.ingestion.feed_handler import PollFeedHandler
from primary_signal.ingestion.feed_polls import FeedFetchResult, FeedPollTarget
from primary_signal.ingestion.models import Article, ContentVersion, FetchAttempt
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.transactions import TransactionalJobQueue
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.extract import extract_article_html

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "public_live_probe_fixture_only"  # noqa: S105  # pragma: allowlist secret


@dataclass
class SyntheticProbe:
    engine: Engine
    database: str
    role: str
    environment: dict[str, str] = field(repr=False)
    sources: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    selected: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def run(
        self, mode: str, *, class_key: str | None = None, rejected: bool = False
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "mode": mode,
            "database": self.database,
            "migration_role": self.role,
            "sources": self.sources,
            "selected": self.selected,
        }
        if class_key is not None:
            params["class_key"] = class_key
        result = subprocess.run(  # noqa: S603 - fixed interpreter/probe, private stdin only.
            [sys.executable, "-c", LIVE_DATABASE_PROBE],
            input=json.dumps(params),
            env=self.environment,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if rejected:
            assert result.returncode != 0, "synthetic invalid evidence was accepted"
            assert not result.stdout, "failed proof must not emit success evidence"
            return {}
        assert result.returncode == 0, "synthetic database proof failed"
        return json.loads(result.stdout)


@pytest.fixture
def probe(tmp_path: Path) -> Iterator[SyntheticProbe]:
    raw = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if (
        not raw
        or not expected
        or os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true"
    ):
        pytest.skip("requires an explicitly disposable PostgreSQL gate cluster")
    passfile = tmp_path / "synthetic-empty.pgpass"
    passfile.write_text("")
    passfile.chmod(0o600)
    base = make_url(raw)
    admin_url = base.set(drivername="postgresql").render_as_string(hide_password=False)
    marker = uuid.uuid4().hex
    role, database = f"live_probe_{marker}", f"live_probe_{marker}_test"
    role_created = database_created = False
    engine: Engine | None = None
    with psycopg.connect(
        admin_url,
        autocommit=True,
        connect_timeout=3,
        options="-c statement_timeout=5000",
        passfile=str(passfile),
    ) as admin:
        assert admin.execute("SELECT current_user").fetchone() == (expected,)
        row = admin.execute(
            "SELECT current_database(), rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert row is not None and row[0].endswith("_test") and row[1] is True
        try:
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role), sql.Literal(PASSWORD))
            )
            role_created = True
            admin.execute(
                sql.SQL("GRANT primary_signal_public_owner TO {}").format(sql.Identifier(role))
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            database_created = True
            admin.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO primary_signal_public_owner").format(
                    sql.Identifier(database)
                )
            )
            url = base.set(username=role, password=PASSWORD, database=database)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("PG", "PRIMARY_SIGNAL_"))
            }
            environment.update(
                PRIMARY_SIGNAL_DATABASE_URL=url.render_as_string(hide_password=False),
                PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE=role,
                PGPASSFILE=str(passfile),
            )
            migrated = subprocess.run(
                [sys.executable, "-m", "primary_signal.entrypoints.migrate"],
                env=environment,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert migrated.returncode == 0, "synthetic non-superuser migration failed"
            engine = create_engine(
                url, hide_parameters=True, connect_args={"passfile": str(passfile)}
            )
            sources = [
                {
                    "class_key": key,
                    "source_key": f"synthetic-{key}",
                    "name": f"Synthetic {key}",
                    "feed_url": f"https://{key}.public.example/feed.xml",
                    "article_host": f"{key}.public.example",
                    "source_id": str(uuid.uuid4()),
                    "feed_id": str(uuid.uuid4()),
                    "feed_job_id": str(uuid.uuid4()),
                }
                for key in ("authority", "vendor", "research")
            ]
            yield SyntheticProbe(engine, database, role, environment, sources)
        finally:
            if engine is not None:
                engine.dispose()
            if database_created:
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
                )
            if role_created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


class SyntheticFeeds:
    def fetch(self, target: FeedPollTarget) -> FeedFetchResult:
        host = target.url.split("/")[2]
        body = (
            "<rss><channel>"
            + "".join(
                f"<item><guid>synthetic-{name}</guid><link>https://{host}/{name}</link>"
                f"<title>Synthetic {name}</title><pubDate>{date}</pubDate></item>"
                for name, date in (
                    ("selected", "Wed, 28 Oct 2026 12:00:00 GMT"),
                    ("deferred", "Tue, 27 Oct 2026 12:00:00 GMT"),
                )
            )
            + "</channel></rss>"
        ).encode()
        return FeedFetchResult(200, target.url, body)


class SyntheticArticles:
    def __init__(self, title: str, body: str) -> None:
        self.body = f"<html><title>{title}</title><article>{body}</article></html>".encode()

    def fetch_article(self, url: str) -> ArticleFetchResult:
        assert url.endswith("/selected")
        return ArticleFetchResult(
            200,
            url,
            (),
            "text/html",
            len(self.body),
            hashlib.sha256(self.body).hexdigest(),
            extract_article_html(self.body, "text/html"),
            None,
            None,
        )


def _poll_sources(probe: SyntheticProbe) -> TransactionalJobQueue:
    assert probe.run("seed") == {"seeded": 3}
    queue = TransactionalJobQueue(probe.engine, build_default_catalogue())
    handler = PollFeedHandler(probe.engine, SyntheticFeeds())
    for source in probe.sources:
        key = source["class_key"]
        assert probe.run("activate_feed", class_key=key) == {"activated": 1}
        lease = queue.claim(queue="ingestion", worker_id=f"synthetic:{uuid.uuid4()}")
        assert lease is not None and str(lease.job_id) == source["feed_job_id"]
        queue.succeed(lease, on_success=handler(lease))
        assert probe.run("poll", class_key=key) == {"poll_passed": 1}
    probe.selected = probe.run("select")["selected"]
    assert len(probe.selected) == 3
    return queue


def _retrieve(
    probe: SyntheticProbe, queue: TransactionalJobQueue, key: str, title: str, body: str
) -> None:
    assert probe.run("activate_article", class_key=key) == {"activated": 1}
    chosen = next(item for item in probe.selected if item["class_key"] == key)
    lease = queue.claim(queue="retrieval", worker_id=f"synthetic:{uuid.uuid4()}")
    assert lease is not None and str(lease.job_id) == chosen["job_id"]
    handler = ArticleRetrievalHandler(probe.engine, SyntheticArticles(title, body))
    queue.succeed(lease, on_success=handler(lease))


@pytest.mark.postgres
def test_live_probe_proves_three_synthetic_classes_body_search_and_deferred_jobs(
    probe: SyntheticProbe,
) -> None:
    queue = _poll_sources(probe)
    assert probe.run("counts") == {"feed_attempts": 3, "article_attempts": 0}
    for source in probe.sources:
        key = source["class_key"]
        _retrieve(
            probe,
            queue,
            key,
            "Synthetic report",
            "Quartz cobalt bulletin provides synthetic provenance evidence.",
        )
        assert probe.run("verify_article", class_key=key) == {
            "article_passed": 1,
            "search_passed": 1,
        }
        probe.run("activate_article", class_key=key, rejected=True)
    assert probe.run("complete") == {
        "feed_attempts": 3,
        "article_attempts": 3,
        "classes_passed": 3,
        "search_checks": 3,
    }
    with probe.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(Article)).scalar_one() == 6
        assert (
            connection.execute(select(func.count()).select_from(ContentVersion)).scalar_one() == 3
        )
        assert connection.execute(select(func.count()).select_from(FetchAttempt)).scalar_one() == 3
        deferred = connection.execute(
            select(Job.id, Job.attempt_count, Job.run_after).where(
                Job.queue == "retrieval", Job.status == "queued"
            )
        ).all()
        assert len(deferred) == 3 and all(row.attempt_count == 0 for row in deferred)
        assert (
            connection.execute(
                select(func.count())
                .select_from(JobAttempt)
                .where(JobAttempt.job_id.in_([row.id for row in deferred]))
            ).scalar_one()
            == 0
        )
    assert queue.claim(queue="retrieval", worker_id=f"synthetic:{uuid.uuid4()}") is None


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Access denied", "Synthetic quartz challenge response."),
        ("Synthetic report", "Please enable JavaScript and cookies before proceeding."),
        ("Synthetic identical title", "Synthetic identical title"),
    ],
)
def test_live_probe_rejects_challenges_and_title_only_search(
    probe: SyntheticProbe, title: str, body: str
) -> None:
    queue = _poll_sources(probe)
    _retrieve(probe, queue, "authority", title, body)
    probe.run("verify_article", class_key="authority", rejected=True)
    assert probe.run("counts") == {"feed_attempts": 3, "article_attempts": 1}
