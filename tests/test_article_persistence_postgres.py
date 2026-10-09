"""PostgreSQL proof of immutable article versions and attempt finalization."""

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, insert, select, text, update
from sqlalchemy.exc import OperationalError

from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.article_results import (
    ArticleRecordSummary,
    ArticleRetrievalRepository,
)
from primary_signal.ingestion.models import Article, ArticleUrl, ContentVersion, FetchAttempt
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import RetrieveArticleV1
from primary_signal.jobs.models import Job
from primary_signal.jobs.repository import JobRepository
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.extract import extract_article_html
from primary_signal.sources.models import Source


@pytest.mark.postgres
def test_article_versions_and_attempts_are_persisted_atomically(
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

    source_id, article_id, url_id = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    article_url = f"https://public.example/article/{article_id}"
    identity = identify_url(article_url)
    now = datetime.now(UTC)

    def result(body: bytes) -> ArticleFetchResult:
        return ArticleFetchResult(
            status=200,
            final_url=article_url,
            redirect_chain=(),
            content_type="text/html; charset=utf-8",
            decoded_byte_count=len(body),
            raw_response_hash=hashlib.sha256(body).hexdigest(),
            extraction=extract_article_html(body, "text/html; charset=utf-8"),
            etag=None,
            last_modified=None,
        )

    first = result(b"<html><title>Notice</title><article>First text.</article></html>")
    second = result(b"<html><title>Notice</title><article>Revised text.</article></html>")
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    insert(Source).values(
                        id=source_id,
                        source_key=f"article-test-{source_id.hex}",
                        name="Synthetic article source",
                        homepage_url="https://public.example/",
                    )
                )
                connection.execute(
                    insert(Article).values(
                        id=article_id,
                        source_id=source_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                connection.execute(
                    insert(ArticleUrl).values(
                        id=url_id,
                        article_id=article_id,
                        original_url=article_url,
                        normalized_url=identity.normalized_url,
                        normalized_url_hash=identity.url_hash,
                        normalization_version=identity.normalization_version,
                        kind="submitted",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_canonical_url_id=url_id)
                )
                repository = ArticleRetrievalRepository(connection)
                target = repository.load_target(article_id, url_id)
                queue = JobRepository(connection, build_default_catalogue())
                summaries: list[ArticleRecordSummary] = []
                for index, observation in enumerate((first, first, second)):
                    job = queue.enqueue(
                        job_type="articles.retrieve",
                        payload_version=1,
                        payload=RetrieveArticleV1(article_id=article_id, article_url_id=url_id),
                    )
                    summaries.append(
                        repository.record_result(
                            target=target,
                            job_id=job.job_id,
                            result=observation,
                            started_at=now + timedelta(minutes=index),
                            completed_at=now + timedelta(minutes=index, seconds=1),
                        )
                    )
                assert [summary.stored for summary in summaries] == [True, False, True]
                assert summaries[0].content_version_id == summaries[1].content_version_id
                assert summaries[0].content_version_id != summaries[2].content_version_id
                assert (
                    connection.execute(
                        select(Article.current_content_version_id).where(Article.id == article_id)
                    ).scalar_one()
                    == summaries[2].content_version_id
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(ContentVersion)
                        .where(ContentVersion.article_id == article_id)
                    ).scalar_one()
                    == 2
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FetchAttempt)
                        .where(
                            FetchAttempt.article_id == article_id,
                            FetchAttempt.status == "fetched",
                        )
                    ).scalar_one()
                    == 3
                )
                job = queue.enqueue(
                    job_type="articles.retrieve",
                    payload_version=1,
                    payload=RetrieveArticleV1(article_id=article_id, article_url_id=url_id),
                )
                repository.record_failure(
                    target=target,
                    job_id=job.job_id,
                    error_code="dependency_timeout",
                    started_at=now + timedelta(minutes=4),
                    completed_at=now + timedelta(minutes=4, seconds=1),
                )
                assert (
                    connection.execute(
                        select(func.count())
                        .select_from(FetchAttempt)
                        .where(
                            FetchAttempt.article_id == article_id,
                            FetchAttempt.status == "failed",
                            FetchAttempt.error_code == "dependency_timeout",
                        )
                    ).scalar_one()
                    == 1
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_source_disable_and_article_persistence_serialize(
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

    source_id, article_id, url_id = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    article_url = f"https://public.example/article/{article_id}"
    identity = identify_url(article_url)
    now = datetime.now(UTC)
    body = b"<html><title>Notice</title><article>Synthetic text.</article></html>"
    result = ArticleFetchResult(
        status=200,
        final_url=article_url,
        redirect_chain=(),
        content_type="text/html; charset=utf-8",
        decoded_byte_count=len(body),
        raw_response_hash=hashlib.sha256(body).hexdigest(),
        extraction=extract_article_html(body, "text/html; charset=utf-8"),
        etag=None,
        last_modified=None,
    )
    job_id: uuid.UUID | None = None
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(Source).values(
                    id=source_id,
                    source_key=f"disable-race-{source_id.hex}",
                    name="Synthetic article source",
                    homepage_url="https://public.example/",
                )
            )
            connection.execute(
                insert(Article).values(
                    id=article_id, source_id=source_id, first_seen_at=now, last_seen_at=now
                )
            )
            connection.execute(
                insert(ArticleUrl).values(
                    id=url_id,
                    article_id=article_id,
                    original_url=article_url,
                    normalized_url=identity.normalized_url,
                    normalized_url_hash=identity.url_hash,
                    normalization_version=identity.normalization_version,
                    kind="submitted",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            connection.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(current_canonical_url_id=url_id)
            )
            target = ArticleRetrievalRepository(connection).load_target(article_id, url_id)
            job = JobRepository(connection, build_default_catalogue()).enqueue(
                job_type="articles.retrieve",
                payload_version=1,
                payload=RetrieveArticleV1(article_id=article_id, article_url_id=url_id),
            )
            job_id = job.job_id

        with engine.connect() as disabling, engine.connect() as persisting:
            # Disable wins: the fetch waits, then sees disabled and writes nothing.
            disabling.execute(update(Source).where(Source.id == source_id).values(enabled=False))
            persisting.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                ArticleRetrievalRepository(persisting).record_result(
                    target=target,
                    job_id=job.job_id,
                    result=result,
                    started_at=now,
                    completed_at=now + timedelta(seconds=1),
                )
            persisting.rollback()
            disabling.commit()
            skipped = ArticleRetrievalRepository(persisting).record_result(
                target=target,
                job_id=job.job_id,
                result=result,
                started_at=now,
                completed_at=now + timedelta(seconds=1),
            )
            ArticleRetrievalRepository(persisting).record_failure(
                target=target,
                job_id=job.job_id,
                error_code="dependency_timeout",
                started_at=now,
                completed_at=now + timedelta(seconds=1),
            )
            assert skipped == ArticleRecordSummary("skipped", False, None)
            assert (
                persisting.execute(
                    select(func.count())
                    .select_from(FetchAttempt)
                    .where(FetchAttempt.article_id == article_id)
                ).scalar_one()
                == 0
            )
            assert (
                persisting.execute(
                    select(func.count())
                    .select_from(ContentVersion)
                    .where(ContentVersion.article_id == article_id)
                ).scalar_one()
                == 0
            )
            persisting.commit()

            # Persistence wins: disabling waits until the fetched version commits.
            disabling.execute(update(Source).where(Source.id == source_id).values(enabled=True))
            disabling.commit()
            fetched = ArticleRetrievalRepository(persisting).record_result(
                target=target,
                job_id=job.job_id,
                result=result,
                started_at=now + timedelta(minutes=1),
                completed_at=now + timedelta(minutes=1, seconds=1),
            )
            assert fetched.status == "fetched"
            disabling.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                disabling.execute(
                    update(Source).where(Source.id == source_id).values(enabled=False)
                )
            disabling.rollback()
            persisting.commit()
            disabling.execute(update(Source).where(Source.id == source_id).values(enabled=False))
            disabling.commit()

        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(FetchAttempt)
                    .where(FetchAttempt.article_id == article_id)
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(ContentVersion)
                    .where(ContentVersion.article_id == article_id)
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    select(Source.enabled).where(Source.id == source_id)
                ).scalar_one()
                is False
            )
    finally:
        # These fixtures are committed so independent connections can see them.
        with engine.begin() as connection:
            connection.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(current_canonical_url_id=None, current_content_version_id=None)
            )
            connection.execute(
                update(FetchAttempt)
                .where(FetchAttempt.article_id == article_id)
                .values(resulting_content_version_id=None)
            )
            connection.execute(
                delete(ContentVersion).where(ContentVersion.article_id == article_id)
            )
            connection.execute(delete(FetchAttempt).where(FetchAttempt.article_id == article_id))
            connection.execute(delete(ArticleUrl).where(ArticleUrl.article_id == article_id))
            connection.execute(delete(Article).where(Article.id == article_id))
            connection.execute(delete(Source).where(Source.id == source_id))
            if job_id is not None:
                connection.execute(delete(Job).where(Job.id == job_id))
        engine.dispose()
