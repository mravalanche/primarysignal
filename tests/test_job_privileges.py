"""PostgreSQL integration tests for least-privilege queue roles."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.registry import build_default_registry
from primary_signal.jobs.repository import JobRepository

SUBMIT_ROLE = "primary_signal_cap_queue_submit"
CONSUME_ROLE = "primary_signal_cap_queue_consume"


@pytest.fixture
def privilege_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Migrate an explicitly disposable database whose capability roles exist."""

    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip(
            "set PRIMARY_SIGNAL_TEST_DATABASE_URL and "
            "PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE for PostgreSQL integration tests"
        )

    engine = create_engine(url)
    with engine.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()")).scalar_one()
        assert str(database_name).endswith("_test"), (
            "privilege integration tests require a disposable database ending in _test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
        roles = connection.execute(
            text(
                "SELECT rolname FROM pg_catalog.pg_roles "
                "WHERE rolname IN (:submit_role, :consume_role)"
            ),
            {"submit_role": SUBMIT_ROLE, "consume_role": CONSUME_ROLE},
        ).scalars()
        assert set(roles) == {SUBMIT_ROLE, CONSUME_ROLE}, (
            "run scripts/bootstrap_test_database_roles.py before migrations"
        )

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        yield engine
    finally:
        engine.dispose()


def _set_role(connection: Connection, role: str) -> None:
    if role not in {SUBMIT_ROLE, CONSUME_ROLE}:
        raise ValueError("unexpected test role")
    connection.execute(text(f"SET LOCAL ROLE {role}"))


def _registry():
    return build_default_registry(
        poll_feed=lambda payload: None,
        retrieve_article=lambda payload: None,
    )


def _assert_bootstrap_refuses_contaminated_role(
    privilege_engine: Engine,
    setup_statements: tuple[str, ...],
) -> None:
    bootstrap_sql = (
        Path(__file__).resolve().parents[1]
        / "deploy/postgres/initdb/010_queue_capability_roles.sql"
    ).read_text(encoding="utf-8")
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    command.downgrade(config, "20261001_02")
    try:
        with privilege_engine.connect() as connection:
            transaction = connection.begin()
            try:
                for statement in setup_statements:
                    connection.execute(text(statement))
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.exec_driver_sql(bootstrap_sql)
            finally:
                transaction.rollback()
    finally:
        command.upgrade(config, "head")


@pytest.mark.postgres
@pytest.mark.parametrize(
    "setup_statements",
    [
        ("GRANT pg_read_all_data TO primary_signal_cap_queue_submit",),
        (
            "CREATE TABLE primary_signal.bootstrap_acl_probe (id integer)",
            "GRANT SELECT ON primary_signal.bootstrap_acl_probe TO primary_signal_cap_queue_submit",
        ),
    ],
)
def test_role_bootstrap_refuses_membership_or_direct_acl_squatting(
    privilege_engine: Engine,
    setup_statements: tuple[str, ...],
) -> None:
    _assert_bootstrap_refuses_contaminated_role(privilege_engine, setup_statements)


@pytest.mark.postgres
def test_queue_capability_roles_are_non_login_non_owner(privilege_engine: Engine) -> None:
    with privilege_engine.connect() as connection:
        role_rows = connection.execute(
            text(
                "SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls FROM pg_catalog.pg_roles "
                "WHERE rolname IN (:submit_role, :consume_role) ORDER BY rolname"
            ),
            {"submit_role": SUBMIT_ROLE, "consume_role": CONSUME_ROLE},
        ).mappings()
        for role in role_rows:
            assert not role["rolcanlogin"]
            assert not role["rolsuper"]
            assert not role["rolinherit"]
            assert not role["rolcreatedb"]
            assert not role["rolcreaterole"]
            assert not role["rolreplication"]
            assert not role["rolbypassrls"]

        owned_objects = connection.execute(
            text(
                "SELECT count(*) FROM pg_catalog.pg_class AS class "
                "JOIN pg_catalog.pg_roles AS role ON role.oid = class.relowner "
                "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = 'primary_signal' "
                "AND role.rolname IN (:submit_role, :consume_role)"
            ),
            {"submit_role": SUBMIT_ROLE, "consume_role": CONSUME_ROLE},
        ).scalar_one()
        assert owned_objects == 0
        owned_schemas_or_functions = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM pg_catalog.pg_namespace AS namespace "
                " JOIN pg_catalog.pg_roles AS role ON role.oid = namespace.nspowner "
                " WHERE namespace.nspname = 'primary_signal' "
                " AND role.rolname IN (:submit_role, :consume_role)) + "
                "(SELECT count(*) FROM pg_catalog.pg_proc AS function "
                " JOIN pg_catalog.pg_roles AS role ON role.oid = function.proowner "
                " JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace "
                " WHERE namespace.nspname = 'primary_signal' "
                " AND role.rolname IN (:submit_role, :consume_role))"
            ),
            {"submit_role": SUBMIT_ROLE, "consume_role": CONSUME_ROLE},
        ).scalar_one()
        assert owned_schemas_or_functions == 0


@pytest.mark.postgres
def test_public_has_no_application_schema_object_or_default_access(
    privilege_engine: Engine,
) -> None:
    with privilege_engine.connect() as connection:
        public_access = connection.execute(
            text(
                "SELECT "
                "has_schema_privilege('public', 'primary_signal', 'USAGE') OR "
                "has_table_privilege('public', 'primary_signal.jobs', 'SELECT') OR "
                "has_function_privilege("
                "'public', 'primary_signal.reject_terminal_history_change()', 'EXECUTE')"
            )
        ).scalar_one()
        assert not public_access

        public_default_access = connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_default_acl AS defaults "
                "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace "
                "CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS privilege "
                "WHERE namespace.nspname = 'primary_signal' AND privilege.grantee = 0 "
                "AND privilege.privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE', "
                "'TRUNCATE', 'REFERENCES', 'TRIGGER', 'USAGE', 'EXECUTE'))"
            )
        ).scalar_one()
        assert not public_default_access


@pytest.mark.postgres
def test_capability_grants_round_trip_without_restoring_public_access(
    privilege_engine: Engine,
) -> None:
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    try:
        command.downgrade(config, "20261001_02")
        with privilege_engine.connect() as connection:
            submit_can_select = connection.execute(
                text("SELECT has_column_privilege(:role, 'primary_signal.jobs', 'id', 'SELECT')"),
                {"role": SUBMIT_ROLE},
            ).scalar_one()
            public_can_use_schema = connection.execute(
                text("SELECT has_schema_privilege('public', 'primary_signal', 'USAGE')")
            ).scalar_one()
            assert not submit_can_select
            assert not public_can_use_schema

        command.upgrade(config, "head")
        with privilege_engine.connect() as connection:
            assert connection.execute(
                text("SELECT has_column_privilege(:role, 'primary_signal.jobs', 'id', 'SELECT')"),
                {"role": SUBMIT_ROLE},
            ).scalar_one()
    finally:
        command.upgrade(config, "head")


@pytest.mark.postgres
def test_submit_role_can_enqueue_and_find_but_cannot_consume(privilege_engine: Engine) -> None:
    with privilege_engine.connect() as connection:
        transaction = connection.begin()
        try:
            _set_role(connection, SUBMIT_ROLE)
            repository = JobRepository(connection, _registry())
            dedupe = f"test:{uuid.uuid7()}"
            first = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid7()),
                deduplication_key=dedupe,
            )
            second = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid7()),
                deduplication_key=dedupe,
            )
            assert first.created
            assert not second.created
            assert second.job_id == first.job_id
            found = connection.execute(
                text(
                    "SELECT id, queue, job_type, deduplication_key, status "
                    "FROM primary_signal.jobs WHERE id=:id"
                ),
                {"id": first.job_id},
            ).one()
            assert found.id == first.job_id
            assert found.status == "queued"

            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("SELECT payload FROM primary_signal.jobs WHERE id=:id"),
                    {"id": first.job_id},
                )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(text("SELECT id FROM primary_signal.job_attempts"))
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("UPDATE primary_signal.jobs SET status='cancelled' WHERE id=:id"),
                    {"id": first.job_id},
                )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("DELETE FROM primary_signal.jobs WHERE id=:id"),
                    {"id": first.job_id},
                )
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_consume_role_can_run_lifecycle_but_cannot_enqueue_or_admin(
    privilege_engine: Engine,
) -> None:
    job_id = uuid.uuid7()
    worker_id = f"processor:{uuid.uuid4()}"
    with privilege_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    "INSERT INTO primary_signal.jobs "
                    "(id, job_type, payload_version, payload, queue, priority, status, run_after) "
                    "VALUES (:id, 'feeds.poll', 1, jsonb_build_object('feed_id', :feed_id), "
                    "'ingestion', 0, 'queued', clock_timestamp())"
                ),
                {"id": job_id, "feed_id": str(uuid.uuid7())},
            )
            _set_role(connection, CONSUME_ROLE)
            repository = JobRepository(connection, _registry())
            lease = repository.claim(queue="ingestion", worker_id=worker_id)
            assert lease is not None
            assert lease.job_id == job_id
            repository.succeed(lease)

            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("SELECT last_error_detail FROM primary_signal.jobs WHERE id=:id"),
                    {"id": job_id},
                )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("UPDATE primary_signal.job_attempts SET worker_id='changed' WHERE id=:id"),
                    {"id": lease.attempt_id},
                )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO primary_signal.jobs "
                        "(id, job_type, payload_version, payload, queue, run_after) "
                        "VALUES (:id, 'feeds.poll', 1, '{}', 'ingestion', now())"
                    ),
                    {"id": uuid.uuid7()},
                )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text("UPDATE primary_signal.jobs SET priority=1 WHERE id=:id"), {"id": job_id}
                )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(text("TRUNCATE primary_signal.jobs"))
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(text("CREATE TABLE primary_signal.forbidden (id integer)"))
        finally:
            transaction.rollback()
