"""PostgreSQL integration tests for the feed-scheduling capability."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

import pytest
from alembic import command
from alembic.config import Config
from psycopg import Connection as PsycopgConnection
from psycopg import Error as PsycopgError
from psycopg import sql
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.transactions import TransactionalJobQueue
from primary_signal.sources.scheduling import TransactionalFeedScheduler

FEED_SCHEDULE_ROLE = "primary_signal_cap_feed_schedule"
SCHEDULER_LOGIN = "scheduler_test"
PROCESSOR_LOGIN = "processor_test"


@pytest.fixture
def feed_privilege_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Migrate an explicitly disposable database with capability roles."""

    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip(
            "set PRIMARY_SIGNAL_TEST_DATABASE_URL and "
            "PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE for PostgreSQL integration tests"
        )

    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as connection:
        database_name, current_user = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        assert str(database_name).endswith("_test")
        assert current_user == expected_role
        assert connection.execute(
            text("SELECT to_regrole(:role) IS NOT NULL"), {"role": FEED_SCHEDULE_ROLE}
        ).scalar_one(), "run scripts/bootstrap_test_database_roles.py before migrations"

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def feed_restricted_engines(
    feed_privilege_engine: Engine,
) -> Iterator[tuple[Engine, Engine]]:
    """Connect as the synthetic scheduler and processor logins."""

    scheduler_url = os.environ.get("PRIMARY_SIGNAL_TEST_SCHEDULER_DATABASE_URL")
    processor_url = os.environ.get("PRIMARY_SIGNAL_TEST_PROCESSOR_DATABASE_URL")
    if not scheduler_url or not processor_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires both restricted login DSNs")
        pytest.skip("set both restricted login DSNs for PostgreSQL privilege tests")

    scheduler = create_engine(scheduler_url, hide_parameters=True)
    processor = create_engine(processor_url, hide_parameters=True)
    try:
        for engine, role in ((scheduler, SCHEDULER_LOGIN), (processor, PROCESSOR_LOGIN)):
            with engine.connect() as connection:
                assert connection.execute(text("SELECT current_user")).scalar_one() == role
        yield scheduler, processor
    finally:
        scheduler.dispose()
        processor.dispose()


@pytest.mark.postgres
def test_feed_schedule_role_is_inert_non_login_capability(
    feed_privilege_engine: Engine,
) -> None:
    with feed_privilege_engine.connect() as connection:
        role = (
            connection.execute(
                text(
                    "SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole, "
                    "rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=:role"
                ),
                {"role": FEED_SCHEDULE_ROLE},
            )
            .mappings()
            .one()
        )
        assert not role["rolcanlogin"]
        assert not role["rolsuper"]
        assert not role["rolinherit"]
        assert not role["rolcreatedb"]
        assert not role["rolcreaterole"]
        assert not role["rolreplication"]
        assert not role["rolbypassrls"]

        outgoing_memberships = connection.execute(
            text(
                "SELECT count(*) FROM pg_catalog.pg_auth_members AS membership "
                "JOIN pg_catalog.pg_roles AS member ON member.oid=membership.member "
                "WHERE member.rolname=:role"
            ),
            {"role": FEED_SCHEDULE_ROLE},
        ).scalar_one()
        owned_or_acl_dependencies = connection.execute(
            text(
                "SELECT count(*) FROM pg_catalog.pg_shdepend AS dependency "
                "JOIN pg_catalog.pg_roles AS role ON role.oid=dependency.refobjid "
                "WHERE dependency.refclassid='pg_catalog.pg_authid'::regclass "
                "AND role.rolname=:role AND dependency.deptype='o'"
            ),
            {"role": FEED_SCHEDULE_ROLE},
        ).scalar_one()
        assert outgoing_memberships == 0
        assert owned_or_acl_dependencies == 0


@pytest.mark.postgres
def test_feed_schedule_grants_round_trip_without_public_access(
    feed_privilege_engine: Engine,
) -> None:
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    try:
        command.downgrade(config, "20261001_03")
        with feed_privilege_engine.connect() as connection:
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.feeds', "
                    "'next_poll_at', 'UPDATE')"
                ),
                {"role": FEED_SCHEDULE_ROLE},
            ).scalar_one()
            assert not connection.execute(
                text(
                    "SELECT has_column_privilege('public', 'primary_signal.feeds', 'id', 'SELECT')"
                )
            ).scalar_one()

        command.upgrade(config, "head")
        with feed_privilege_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.feeds', "
                    "'next_poll_at', 'UPDATE')"
                ),
                {"role": FEED_SCHEDULE_ROLE},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'primary_signal.sources', "
                    "'enabled', 'SELECT')"
                ),
                {"role": FEED_SCHEDULE_ROLE},
            ).scalar_one()
    finally:
        command.upgrade(config, "head")


@pytest.mark.postgres
@pytest.mark.parametrize(
    "contamination",
    [
        "GRANT pg_read_all_data TO primary_signal_cap_feed_schedule",
        "GRANT SELECT ON primary_signal.feeds TO primary_signal_cap_feed_schedule",
        "ALTER TABLE primary_signal.feeds OWNER TO primary_signal_cap_feed_schedule",
    ],
)
def test_feed_role_bootstrap_refuses_membership_acl_or_ownership_squatting(
    feed_privilege_engine: Engine,
    contamination: str,
) -> None:
    bootstrap_sql = (
        Path(__file__).resolve().parents[1]
        / "deploy/postgres/initdb/020_feed_scheduler_capability_role.sql"
    ).read_text(encoding="utf-8")
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    command.downgrade(config, "20261001_03")
    try:
        with feed_privilege_engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    text("REVOKE primary_signal_cap_feed_schedule FROM scheduler_test")
                )
                connection.execute(text(contamination))
                driver = cast(PsycopgConnection[Any], connection.connection.driver_connection)
                with (
                    pytest.raises(
                        PsycopgError,
                        match="not an unused Primary Signal capability role",
                    ),
                    connection.begin_nested(),
                    driver.cursor() as cursor,
                ):
                    cursor.execute(sql.SQL(cast(LiteralString, bootstrap_sql)))
            finally:
                transaction.rollback()
    finally:
        command.upgrade(config, "head")


@pytest.mark.postgres
def test_scheduler_can_advance_due_feeds_and_enqueue_only(
    feed_privilege_engine: Engine,
    feed_restricted_engines: tuple[Engine, Engine],
) -> None:
    scheduler_engine, processor_engine = feed_restricted_engines
    source_id = uuid.uuid7()
    feed_id = uuid.uuid7()
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    source_key = f"privilege-test-{uuid.uuid4().hex}"

    with feed_privilege_engine.begin() as admin:
        admin.execute(
            text(
                "INSERT INTO primary_signal.sources "
                "(id, source_key, name, homepage_url, enabled) "
                "VALUES (:id, :source_key, 'Privilege test', "
                "'https://public.example/', true)"
            ),
            {"id": source_id, "source_key": source_key},
        )
        admin.execute(
            text(
                "INSERT INTO primary_signal.feeds "
                "(id, source_id, name, configured_url, normalized_url, url_hash, "
                "url_normalization_version, enabled, poll_interval_seconds, next_poll_at) "
                "VALUES (:id, :source_id, 'Privilege feed', "
                "'https://public.example/feed', 'https://public.example/feed', "
                ":url_hash, 1, true, 900, :next_poll_at)"
            ),
            {
                "id": feed_id,
                "source_id": source_id,
                "url_hash": uuid.uuid4().hex * 2,
                "next_poll_at": due_at,
            },
        )

    scheduler = TransactionalFeedScheduler(scheduler_engine, build_default_catalogue())
    processor = TransactionalJobQueue(processor_engine, build_default_catalogue())
    job_id: uuid.UUID | None = None
    try:
        summary = scheduler.schedule_due(limit=1)
        assert summary.selected == 1
        assert summary.enqueued == 1
        assert summary.already_active == 0
        with feed_privilege_engine.connect() as admin:
            job_id = cast(
                uuid.UUID,
                admin.execute(
                    text(
                        "SELECT id FROM primary_signal.jobs "
                        "WHERE deduplication_key=:deduplication_key"
                    ),
                    {"deduplication_key": f"feed:{feed_id}"},
                ).scalar_one(),
            )
            advanced_to, updated_at = admin.execute(
                text("SELECT next_poll_at, updated_at FROM primary_signal.feeds WHERE id=:feed_id"),
                {"feed_id": feed_id},
            ).one()
        assert cast(datetime, advanced_to) == cast(datetime, updated_at) + timedelta(minutes=15)
        assert processor.cancel_queued(job_id)

        scheduler_denied = (
            "SELECT configured_url FROM primary_signal.feeds",
            "UPDATE primary_signal.feeds SET enabled=false",
            "UPDATE primary_signal.sources SET enabled=false",
            "DELETE FROM primary_signal.feeds",
        )
        for statement in scheduler_denied:
            with pytest.raises(DBAPIError), scheduler_engine.begin() as connection:
                connection.execute(text(statement))

        processor_denied = (
            "SELECT name FROM primary_signal.feeds",
            "UPDATE primary_signal.feeds SET next_poll_at=clock_timestamp()",
        )
        for statement in processor_denied:
            with pytest.raises(DBAPIError), processor_engine.begin() as connection:
                connection.execute(text(statement))
    finally:
        with feed_privilege_engine.begin() as admin:
            if job_id is not None:
                admin.execute(
                    text("DELETE FROM primary_signal.jobs WHERE id=:job_id"),
                    {"job_id": job_id},
                )
            admin.execute(
                text("DELETE FROM primary_signal.feeds WHERE id=:feed_id"),
                {"feed_id": feed_id},
            )
            admin.execute(
                text("DELETE FROM primary_signal.sources WHERE id=:source_id"),
                {"source_id": source_id},
            )
