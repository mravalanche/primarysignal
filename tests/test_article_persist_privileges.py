"""PostgreSQL privilege checks for extracted article persistence."""

import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.ingestion.article_results import ArticleRetrievalRepository
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.extract import extract_article_html

ROLE = "primary_signal_cap_article_persist"


@pytest.fixture
def article_privilege_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, Engine]]:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    processor_url = os.environ.get("PRIMARY_SIGNAL_TEST_PROCESSOR_DATABASE_URL")
    if not admin_url or not expected_role or not processor_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires the admin and restricted processor DSNs")
        pytest.skip("set test admin and processor DSNs for PostgreSQL privilege tests")

    admin = create_engine(admin_url, hide_parameters=True)
    processor = create_engine(processor_url, hide_parameters=True)
    with admin.connect() as connection:
        database_name, current_user = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        assert str(database_name).endswith("_test")
        assert current_user == expected_role
        assert connection.execute(
            text("SELECT to_regrole(:role) IS NOT NULL"), {"role": ROLE}
        ).scalar_one(), "run scripts/bootstrap_test_database_roles.py before migrations"
    with processor.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == "processor_test"

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        yield admin, processor
    finally:
        admin.dispose()
        processor.dispose()


@pytest.mark.postgres
def test_article_persist_capability_is_non_login_and_column_scoped(
    article_privilege_engines: tuple[Engine, Engine],
) -> None:
    admin, processor = article_privilege_engines
    with admin.connect() as connection:
        role = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=:role"
            ),
            {"role": ROLE},
        ).one()
        assert not any(role)
        assert connection.execute(
            text("SELECT pg_has_role('processor_test', :role, 'member')"),
            {"role": ROLE},
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT has_function_privilege(:role, "
                "'primary_signal.lock_source_enabled(uuid)', 'EXECUTE')"
            ),
            {"role": ROLE},
        ).scalar_one()

        allowed = (
            ("sources", "enabled", "SELECT"),
            ("articles", "source_id", "SELECT"),
            ("articles", "current_canonical_url_id", "SELECT"),
            ("articles", "current_content_version_id", "UPDATE"),
            ("article_urls", "normalized_url", "SELECT"),
            ("fetch_attempts", "requested_url", "INSERT"),
            ("fetch_attempts", "redirect_chain", "INSERT"),
            ("fetch_attempts", "id", "SELECT"),
            ("fetch_attempts", "resulting_content_version_id", "UPDATE"),
            ("fetch_attempts", "error_code", "UPDATE"),
            ("content_versions", "extracted_text", "INSERT"),
            ("content_versions", "normalized_content_hash", "SELECT"),
        )
        denied = (
            ("sources", "name", "SELECT"),
            ("sources", "enabled", "UPDATE"),
            ("articles", "last_seen_at", "UPDATE"),
            ("article_urls", "normalized_url", "UPDATE"),
            ("fetch_attempts", "requested_url", "SELECT"),
            ("fetch_attempts", "article_id", "UPDATE"),
            ("content_versions", "extracted_text", "SELECT"),
            ("content_versions", "extracted_text", "UPDATE"),
            ("jobs", "id", "SELECT"),
        )
        for table, column, privilege in allowed:
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, "
                    "'primary_signal.' || :table, :column, :privilege)"
                ),
                {"role": ROLE, "table": table, "column": column, "privilege": privilege},
            ).scalar_one()
        for table, column, privilege in denied:
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege(:role, "
                    "'primary_signal.' || :table, :column, :privilege)"
                ),
                {"role": ROLE, "table": table, "column": column, "privilege": privilege},
            ).scalar_one()
        for table in ("articles", "fetch_attempts", "content_versions"):
            assert not connection.execute(
                text("SELECT has_table_privilege(:role, 'primary_signal.' || :table, 'DELETE')"),
                {"role": ROLE, "table": table},
            ).scalar_one()

    with processor.connect() as connection:
        assert (
            connection.execute(
                text("SELECT primary_signal.lock_source_enabled(:source_id)"),
                {"source_id": uuid.uuid7()},
            ).scalar_one()
            is False
        )
        assert (
            connection.execute(
                text("SELECT normalized_url FROM primary_signal.article_urls WHERE false")
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                text(
                    "SELECT normalized_content_hash FROM primary_signal.content_versions WHERE false"
                )
            ).fetchall()
            == []
        )

    for statement in (
        "SELECT enabled FROM primary_signal.sources FOR SHARE",
        "SELECT extracted_text FROM primary_signal.content_versions WHERE false",
        "SELECT requested_url FROM primary_signal.fetch_attempts WHERE false",
        "UPDATE primary_signal.articles SET first_seen_at=now() WHERE false",
        "UPDATE primary_signal.fetch_attempts SET requested_url='forbidden' WHERE false",
        "UPDATE primary_signal.content_versions SET extracted_text='forbidden' WHERE false",
        "DELETE FROM primary_signal.fetch_attempts WHERE false",
    ):
        with pytest.raises(DBAPIError), processor.begin() as connection:
            connection.execute(text(statement))


@pytest.mark.postgres
def test_restricted_processor_records_and_deduplicates_extracted_article(
    article_privilege_engines: tuple[Engine, Engine],
) -> None:
    admin, processor = article_privilege_engines
    source_id, article_id, article_url_id, job_id = (uuid.uuid7() for _ in range(4))
    url = f"https://news.public.example/{article_id.hex}"
    now = datetime.now(UTC)
    with admin.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO primary_signal.sources (id, source_key, name, homepage_url) "
                "VALUES (:id, :key, 'Synthetic source', 'https://public.example')"
            ),
            {"id": source_id, "key": f"article-priv-{source_id.hex}"},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.articles (id, source_id, first_seen_at, last_seen_at) "
                "VALUES (:id, :source_id, :now, :now)"
            ),
            {"id": article_id, "source_id": source_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.article_urls "
                "(id, article_id, original_url, normalized_url, normalized_url_hash, "
                "normalization_version, kind, first_seen_at, last_seen_at) "
                "VALUES (:id, :article_id, :url, :url, :hash, 1, 'submitted', :now, :now)"
            ),
            {
                "id": article_url_id,
                "article_id": article_id,
                "url": url,
                "hash": hashlib.sha256(url.encode()).hexdigest(),
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE primary_signal.articles SET current_canonical_url_id=:url_id "
                "WHERE id=:article_id"
            ),
            {"url_id": article_url_id, "article_id": article_id},
        )
        connection.execute(
            text(
                "INSERT INTO primary_signal.jobs "
                "(id, job_type, payload_version, payload, run_after) "
                "VALUES (:id, 'retrieve_article_v1', 1, '{}'::jsonb, :now)"
            ),
            {"id": job_id, "now": now},
        )

    body = b"<html><head><title>Notice</title></head><body><article>Public synthetic report.</article></body></html>"
    result = ArticleFetchResult(
        status=200,
        final_url=url,
        redirect_chain=(),
        content_type="text/html; charset=utf-8",
        decoded_byte_count=len(body),
        raw_response_hash=hashlib.sha256(body).hexdigest(),
        extraction=extract_article_html(body, "text/html; charset=utf-8"),
        etag=None,
        last_modified=None,
    )
    with processor.begin() as connection:
        repository = ArticleRetrievalRepository(connection)
        target = repository.load_target(article_id, article_url_id)
        first = repository.record_result(
            target=target,
            job_id=job_id,
            result=result,
            started_at=now,
            completed_at=now + timedelta(seconds=1),
        )
        second = repository.record_result(
            target=target,
            job_id=job_id,
            result=result,
            started_at=now + timedelta(seconds=2),
            completed_at=now + timedelta(seconds=3),
        )
        assert first.status == "fetched" and first.stored
        assert second.status == "fetched" and not second.stored
        assert second.content_version_id == first.content_version_id


@pytest.mark.postgres
def test_article_persist_grants_round_trip(
    article_privilege_engines: tuple[Engine, Engine],
) -> None:
    admin, _ = article_privilege_engines
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    try:
        command.downgrade(config, "20261007_05")
        with admin.connect() as connection:
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.articles', "
                    "'current_content_version_id', 'UPDATE')"
                ),
                {"role": ROLE},
            ).scalar_one()
        command.upgrade(config, "head")
        with admin.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.articles', "
                    "'current_content_version_id', 'UPDATE')"
                ),
                {"role": ROLE},
            ).scalar_one()
    finally:
        command.upgrade(config, "head")
