"""PostgreSQL integration tests for least-privilege queue roles."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import JobFailure, PollFeedV1
from primary_signal.jobs.repository import FailureDisposition, JobRepository
from primary_signal.jobs.transactions import TransactionalJobQueue

SUBMIT_ROLE = "primary_signal_cap_queue_submit"
CONSUME_ROLE = "primary_signal_cap_queue_consume"
SCHEDULER_LOGIN = "scheduler_test"
PROCESSOR_LOGIN = "processor_test"


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


@pytest.fixture
def restricted_engines(privilege_engine: Engine) -> Iterator[tuple[Engine, Engine]]:
    """Connect as the two synthetic login roles created only for CI."""

    scheduler_url = os.environ.get("PRIMARY_SIGNAL_TEST_SCHEDULER_DATABASE_URL")
    processor_url = os.environ.get("PRIMARY_SIGNAL_TEST_PROCESSOR_DATABASE_URL")
    if not scheduler_url or not processor_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires both restricted login DSNs")
        pytest.skip("set both restricted login DSNs for PostgreSQL privilege tests")

    scheduler = create_engine(scheduler_url, hide_parameters=True)
    processor = create_engine(processor_url, hide_parameters=True)
    try:
        for engine, expected_role in (
            (scheduler, SCHEDULER_LOGIN),
            (processor, PROCESSOR_LOGIN),
        ):
            with engine.connect() as connection:
                database_name, current_user = connection.execute(
                    text("SELECT current_database(), current_user")
                ).one()
                assert str(database_name).endswith("_test")
                assert current_user == expected_role
        yield scheduler, processor
    finally:
        scheduler.dispose()
        processor.dispose()


def _set_role(connection: Connection, role: str) -> None:
    if role not in {SUBMIT_ROLE, CONSUME_ROLE}:
        raise ValueError("unexpected test role")
    connection.execute(text(f"SET LOCAL ROLE {role}"))


def _catalogue():
    return build_default_catalogue()


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
            repository = JobRepository(connection, _catalogue())
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
                    "VALUES (:id, 'feeds.poll', 1, "
                    "jsonb_build_object('feed_id', CAST(:feed_id AS text)), "
                    "'ingestion', 0, 'queued', clock_timestamp())"
                ),
                {"id": job_id, "feed_id": str(uuid.uuid7())},
            )
            _set_role(connection, CONSUME_ROLE)
            repository = JobRepository(connection, _catalogue())
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


@pytest.mark.postgres
def test_restricted_logins_have_only_expected_attributes_and_memberships(
    privilege_engine: Engine,
    restricted_engines: tuple[Engine, Engine],
) -> None:
    scheduler, processor = restricted_engines
    expected_memberships = {
        SCHEDULER_LOGIN: {SUBMIT_ROLE},
        PROCESSOR_LOGIN: {SUBMIT_ROLE, CONSUME_ROLE},
    }
    migrator = os.environ["PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE"]

    with privilege_engine.connect() as admin:
        rows = admin.execute(
            text(
                "SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb, "
                "rolcreaterole, rolreplication, rolbypassrls FROM pg_catalog.pg_roles "
                "WHERE rolname IN (:scheduler, :processor)"
            ),
            {"scheduler": SCHEDULER_LOGIN, "processor": PROCESSOR_LOGIN},
        ).mappings()
        by_name = {str(row["rolname"]): row for row in rows}
        assert set(by_name) == {SCHEDULER_LOGIN, PROCESSOR_LOGIN}
        for role_name, row in by_name.items():
            assert row["rolcanlogin"]
            assert row["rolinherit"]
            assert not row["rolsuper"]
            assert not row["rolcreatedb"]
            assert not row["rolcreaterole"]
            assert not row["rolreplication"]
            assert not row["rolbypassrls"]
            assert not admin.execute(
                text("SELECT pg_has_role(:login, :migrator, 'MEMBER')"),
                {"login": role_name, "migrator": migrator},
            ).scalar_one()

            membership_rows = admin.execute(
                text(
                    "SELECT parent.rolname, membership.admin_option, "
                    "membership.inherit_option, membership.set_option "
                    "FROM pg_catalog.pg_auth_members AS membership "
                    "JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid "
                    "JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member "
                    "WHERE member.rolname = :login"
                ),
                {"login": role_name},
            ).mappings()
            memberships = {
                str(membership["rolname"]): (
                    bool(membership["admin_option"]),
                    bool(membership["inherit_option"]),
                    bool(membership["set_option"]),
                )
                for membership in membership_rows
            }
            assert memberships == {
                capability: (False, True, True) for capability in expected_memberships[role_name]
            }

            owned = admin.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend AS dependency "
                    "JOIN pg_catalog.pg_roles AS role ON role.oid = dependency.refobjid "
                    "WHERE dependency.refclassid = 'pg_catalog.pg_authid'::regclass "
                    "AND role.rolname = :login AND dependency.deptype IN ('a', 'o'))"
                ),
                {"login": role_name},
            ).scalar_one()
            assert not owned

    for engine, expected in ((scheduler, SCHEDULER_LOGIN), (processor, PROCESSOR_LOGIN)):
        with engine.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == expected


@pytest.mark.postgres
def test_actual_scheduler_and_processor_logins_enforce_queue_boundary(
    privilege_engine: Engine,
    restricted_engines: tuple[Engine, Engine],
) -> None:
    scheduler_engine, processor_engine = restricted_engines
    scheduler = TransactionalJobQueue(scheduler_engine, _catalogue())
    processor = TransactionalJobQueue(processor_engine, _catalogue(), random_value=lambda: 0.0)
    created_ids: set[uuid.UUID] = set()

    dedupe = f"restricted-scheduler:{uuid.uuid7()}"
    first = scheduler.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
        deduplication_key=dedupe,
    )
    second = scheduler.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
        deduplication_key=dedupe,
    )
    created_ids.add(first.job_id)
    assert first.created
    assert not second.created
    assert second.job_id == first.job_id
    with pytest.raises(DBAPIError):
        scheduler.claim(queue="ingestion", worker_id=f"scheduler:{uuid.uuid4()}")
    assert processor.cancel_queued(first.job_id)

    retry = processor.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
        max_attempts=2,
    )
    created_ids.add(retry.job_id)
    first_attempt = processor.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert first_attempt is not None
    assert processor.heartbeat(first_attempt) > first_attempt.lease_expires_at
    failure_successors: list[uuid.UUID] = []

    def enqueue_failure_successor(
        _connection: Connection,
        repository: JobRepository,
        _disposition: FailureDisposition,
    ) -> None:
        failure_successors.append(
            repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid7()),
                deduplication_key=f"restricted-failure-successor:{uuid.uuid7()}",
            ).job_id
        )

    first_failure = processor.fail(
        first_attempt,
        JobFailure(code="dependency_timeout"),
        on_failure=enqueue_failure_successor,
    )
    assert first_failure.status == "queued"
    assert first_failure.retry_at is not None
    assert len(failure_successors) == 1
    created_ids.add(failure_successors[0])
    assert processor.cancel_queued(failure_successors[0])
    with privilege_engine.begin() as admin:
        admin.execute(
            text("UPDATE primary_signal.jobs SET run_after=clock_timestamp() WHERE id=:job_id"),
            {"job_id": retry.job_id},
        )
    second_attempt = processor.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert second_attempt is not None
    second_failure = processor.fail(second_attempt, JobFailure(code="dependency_timeout"))
    assert second_failure.status == "dead"
    assert second_failure.retry_at is None

    permanent = processor.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
    )
    created_ids.add(permanent.job_id)
    permanent_lease = processor.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert permanent_lease is not None
    permanent_failure = processor.fail(permanent_lease, JobFailure(code="bad_payload"))
    assert permanent_failure.status == "dead"
    assert permanent_failure.retry_at is None

    expired = processor.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
    )
    created_ids.add(expired.job_id)
    expired_lease = processor.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert expired_lease is not None
    with privilege_engine.begin() as admin:
        admin.execute(
            text(
                "UPDATE primary_signal.jobs "
                "SET lease_expires_at=clock_timestamp() - INTERVAL '1 second' "
                "WHERE id=:job_id"
            ),
            {"job_id": expired.job_id},
        )
    assert processor.recover_expired().retried == 1
    assert processor.cancel_queued(expired.job_id)

    queued = processor.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
    )
    created_ids.add(queued.job_id)
    assert processor.cancel_queued(queued.job_id)

    original = processor.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid7()),
    )
    created_ids.add(original.job_id)
    original_lease = processor.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert original_lease is not None
    successor_ids: list[uuid.UUID] = []

    def enqueue_successor(_connection: Connection, repository: JobRepository) -> None:
        result = repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=uuid.uuid7()),
            deduplication_key=f"restricted-successor:{uuid.uuid7()}",
        )
        successor_ids.append(result.job_id)

    processor.succeed(original_lease, on_success=enqueue_successor)
    assert len(successor_ids) == 1
    created_ids.add(successor_ids[0])
    assert processor.cancel_queued(successor_ids[0])

    with privilege_engine.connect() as admin:
        state_rows = admin.execute(
            text("SELECT id, status FROM primary_signal.jobs WHERE id = ANY(:ids)"),
            {"ids": list(created_ids)},
        ).mappings()
        states = {cast(uuid.UUID, row["id"]): cast(str, row["status"]) for row in state_rows}
    assert states[retry.job_id] == "dead"
    assert states[permanent.job_id] == "dead"
    assert states[original.job_id] == "succeeded"

    denied_statements = (
        "UPDATE primary_signal.job_attempts SET worker_id='changed'",
        "DELETE FROM primary_signal.jobs",
        "TRUNCATE primary_signal.jobs",
        "SELECT id FROM primary_signal.sources",
        "CREATE TABLE primary_signal.forbidden (id integer)",
        "SET ROLE postgres",
    )
    for statement in denied_statements:
        with pytest.raises(DBAPIError), processor_engine.begin() as connection:
            connection.execute(text(statement))
