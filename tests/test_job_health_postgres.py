"""Metadata-only job health through a restricted disposable PostgreSQL login."""

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import Connection as PsycopgConnection
from psycopg import sql
from sqlalchemy import create_engine, delete, insert, text
from sqlalchemy.exc import DBAPIError

from primary_signal.jobs.health import read_health
from primary_signal.jobs.models import Job

ROLE = "primary_signal_cap_job_health"
ALLOWED_COLUMNS = {
    "id",
    "queue",
    "job_type",
    "status",
    "attempt_count",
    "max_attempts",
    "run_after",
    "lease_expires_at",
    "heartbeat_at",
    "created_at",
    "completed_at",
}


@pytest.mark.postgres
def test_job_health_reports_lifecycle_with_restricted_column_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected:
        pytest.skip("set disposable PostgreSQL test database settings")
    health_url = os.environ.get("PRIMARY_SIGNAL_TEST_JOB_HEALTH_DATABASE_URL")
    if not health_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires the restricted job-health login DSN")
        pytest.skip("set restricted job-health login DSN")
    if os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true":
        pytest.skip("requires an explicitly disposable PostgreSQL database")

    engine = create_engine(url, hide_parameters=True)
    health_engine = create_engine(health_url, hide_parameters=True)
    queue = f"health-{uuid.uuid7().hex}"
    other_queue = f"other-{uuid.uuid7().hex}"
    identifiers: list[uuid.UUID] = []
    try:
        with engine.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            assert connection.execute(text("SELECT current_user")).scalar_one() == expected
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected)
        command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

        with engine.begin() as connection:
            now = cast(datetime, connection.execute(text("SELECT clock_timestamp()")).scalar_one())
            # Wide time windows keep status classifications independent of test speed.
            past, future = now - timedelta(hours=1), now + timedelta(hours=1)
            cases = (
                ("queued", 0, past, None),
                ("queued", 2, past, None),
                ("queued", 0, future, None),
                ("queued", 1, future, None),
                ("running", 1, past, future),
                ("running", 1, past, past),
                ("dead", 3, past, None),
                ("succeeded", 1, past, None),
                ("cancelled", 0, past, None),
            )
            for status, attempts, run_after, lease_expires_at in cases:
                identifier = uuid.uuid7()
                identifiers.append(identifier)
                connection.execute(
                    insert(Job).values(
                        id=identifier,
                        queue=queue,
                        job_type="synthetic.health",
                        payload_version=1,
                        payload={"fixture": "synthetic-job-payload"},
                        status=status,
                        attempt_count=attempts,
                        max_attempts=3,
                        run_after=run_after,
                        created_at=now,
                        completed_at=past if status in {"dead", "succeeded", "cancelled"} else None,
                        worker_id="synthetic-worker" if status == "running" else None,
                        lease_token=uuid.uuid7() if status == "running" else None,
                        lease_expires_at=lease_expires_at,
                        heartbeat_at=past if status == "running" else None,
                        deduplication_key=f"synthetic-dedupe-{identifier}",
                        last_error_code="synthetic_error",
                        last_error_detail="synthetic-private-error-detail",
                    )
                )
            other_identifier = uuid.uuid7()
            identifiers.append(other_identifier)
            connection.execute(
                insert(Job).values(
                    id=other_identifier,
                    queue=other_queue,
                    job_type="synthetic.health",
                    payload_version=1,
                    payload={},
                    run_after=past,
                )
            )

        expected_counts = {
            "queue": queue,
            "queued": 4,
            "due": 2,
            "delayed": 2,
            "queued_retries": 2,
            "running": 2,
            "expired": 1,
            "dead": 1,
            "succeeded": 1,
            "cancelled": 1,
        }
        with health_engine.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == "job_health_test"
            report = read_health(connection, queue=queue, limit=100)
            assert report.queues == [expected_counts]
            assert report.queues_truncated is False
            assert report.jobs_truncated is False
            assert [str(job["id"]) for job in report.jobs] == [
                str(identifier) for identifier in sorted(identifiers[:-1], reverse=True)
            ]
            assert {job["status"] for job in report.jobs} == {
                "queued",
                "running",
                "dead",
                "succeeded",
                "cancelled",
            }
            assert all(set(job) == ALLOWED_COLUMNS for job in report.jobs)
            serialized = json.dumps(report.as_dict())
            for forbidden in (
                "synthetic-job-payload",
                "synthetic-worker",
                "synthetic-dedupe",
                "synthetic_error",
                "synthetic-private-error-detail",
            ):
                assert forbidden not in serialized

            # Status narrows detail rows, but queue summaries retain the full queue.
            filtered = read_health(connection, queue=queue, status="queued", limit=2)
            assert filtered.queues == [expected_counts]
            assert len(filtered.jobs) == 2
            assert all(job["status"] == "queued" for job in filtered.jobs)
            assert filtered.jobs_truncated is True
            assert filtered.queues_truncated is False
            empty = read_health(connection, queue=f"missing-{uuid.uuid7().hex}")
            assert empty.queues == [] and empty.jobs == []
            assert empty.queues_truncated is False and empty.jobs_truncated is False
            bounded = read_health(connection, limit=1)
            assert len(bounded.queues) == 1
            assert bounded.queues_truncated is True
            assert len(bounded.jobs) == 1 and bounded.jobs_truncated is True

        for statement in (
            "SELECT * FROM primary_signal.jobs LIMIT 1",
            "SELECT payload FROM primary_signal.jobs LIMIT 1",
            "SELECT payload_version FROM primary_signal.jobs LIMIT 1",
            "SELECT priority FROM primary_signal.jobs LIMIT 1",
            "SELECT last_error_code FROM primary_signal.jobs LIMIT 1",
            "SELECT last_error_detail FROM primary_signal.jobs LIMIT 1",
            "SELECT deduplication_key FROM primary_signal.jobs LIMIT 1",
            "SELECT worker_id FROM primary_signal.jobs LIMIT 1",
            "SELECT lease_token FROM primary_signal.jobs LIMIT 1",
            "SELECT id FROM primary_signal.job_attempts LIMIT 1",
            "SELECT id FROM primary_signal.sources LIMIT 1",
            "SELECT extracted_text FROM primary_signal.content_versions LIMIT 1",
            "INSERT INTO primary_signal.jobs DEFAULT VALUES",
            "UPDATE primary_signal.jobs SET status=status WHERE false",
            "DELETE FROM primary_signal.jobs WHERE false",
        ):
            with pytest.raises(DBAPIError) as caught, health_engine.begin() as connection:
                connection.execute(text(statement))
            assert isinstance(caught.value.orig, psycopg.Error)
            assert caught.value.orig.sqlstate == "42501"

        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT NOT (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole "
                    "OR rolinherit OR rolreplication OR rolbypassrls) "
                    "FROM pg_catalog.pg_roles WHERE rolname=:role"
                ),
                {"role": ROLE},
            ).scalar_one()
            columns = (
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='primary_signal' AND table_name='jobs' "
                        "AND has_column_privilege(:role, 'primary_signal.jobs', column_name, 'SELECT')"
                    ),
                    {"role": ROLE},
                )
                .scalars()
                .all()
            )
            assert set(columns) == ALLOWED_COLUMNS
            assert not connection.execute(
                text(
                    "SELECT has_table_privilege(:role, 'primary_signal.jobs', 'SELECT,INSERT,UPDATE,DELETE')"
                ),
                {"role": ROLE},
            ).scalar_one()
            for login in ("public_test", "publication_test", "editorial_test", "processor_test"):
                assert not connection.execute(
                    text("SELECT pg_has_role(:login, :role, 'USAGE')"),
                    {"login": login, "role": ROLE},
                ).scalar_one()
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Job).where(Job.id.in_(identifiers)))
        health_engine.dispose()
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("contamination", ["attribute", "membership", "acl", "ownership"])
def test_job_health_bootstrap_rejects_existing_role_contamination(contamination: str) -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    if not url or os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true":
        pytest.skip("requires an explicitly disposable PostgreSQL database")
    engine = create_engine(url, hide_parameters=True)
    suffix = uuid.uuid7().hex
    probe_role = f"health_probe_{suffix}"
    member_role = f"health_member_{suffix}"
    probe_schema = f"health_schema_{suffix}"
    bootstrap = (
        (
            Path(__file__).resolve().parents[1]
            / "deploy/postgres/initdb/130_job_health_capability_role.sql"
        )
        .read_text()
        .replace(ROLE, probe_role)
    )
    try:
        with engine.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            transaction = connection.begin_nested()
            try:
                driver = cast(PsycopgConnection[Any], connection.connection.driver_connection)
                with driver.cursor() as cursor:
                    cursor.execute(sql.SQL(cast(LiteralString, bootstrap)))
                    if contamination == "attribute":
                        cursor.execute(
                            sql.SQL("ALTER ROLE {} INHERIT").format(sql.Identifier(probe_role))
                        )
                    elif contamination == "membership":
                        cursor.execute(
                            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(member_role))
                        )
                        cursor.execute(
                            sql.SQL("GRANT {} TO {}").format(
                                sql.Identifier(probe_role), sql.Identifier(member_role)
                            )
                        )
                    elif contamination == "acl":
                        cursor.execute(
                            sql.SQL("GRANT USAGE ON SCHEMA primary_signal TO {}").format(
                                sql.Identifier(probe_role)
                            )
                        )
                    else:
                        cursor.execute(
                            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                                sql.Identifier(probe_schema), sql.Identifier(probe_role)
                            )
                        )
                    with pytest.raises(
                        psycopg.Error, match="not an unused Primary Signal capability"
                    ):
                        cursor.execute(sql.SQL(cast(LiteralString, bootstrap)))
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
