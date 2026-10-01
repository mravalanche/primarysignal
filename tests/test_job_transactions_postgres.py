"""PostgreSQL proof of atomic job completion rollback."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, func, select, text, update

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import JobFailure, PollFeedV1
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.repository import FailureDisposition, JobRepository, LostLease
from primary_signal.jobs.transactions import TransactionalJobQueue


@pytest.fixture
def transaction_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Migrate an explicitly disposable database and provide its engine."""

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
            "job integration tests require a disposable database ending in _test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    command.upgrade(config, "head")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_prepared_callback_failure_rolls_back_completion_and_successor(
    transaction_engine: Engine,
) -> None:
    catalogue = build_default_catalogue()
    queue = TransactionalJobQueue(transaction_engine, catalogue)
    original_id = queue.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    ).job_id
    lease = queue.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert lease is not None
    successor_key = f"rollback-proof:{uuid.uuid4()}"

    def enqueue_successor(
        _connection: Connection,
        repository: JobRepository,
    ) -> None:
        repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=uuid.uuid4()),
            deduplication_key=successor_key,
        )
        raise RuntimeError("prepared mutation failed")

    with pytest.raises(RuntimeError, match="prepared mutation failed"):
        queue.succeed(lease, on_success=enqueue_successor)

    with transaction_engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(Job).where(Job.deduplication_key == successor_key)
            ).scalar_one()
            == 0
        )
        state = connection.execute(
            select(Job.status, Job.lease_token).where(Job.id == original_id)
        ).one()
        assert state == ("running", lease.lease_token)
        connection.execute(
            update(Job)
            .where(Job.id == original_id)
            .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )

    queue.recover_expired()
    assert queue.cancel_queued(original_id)


@pytest.mark.postgres
def test_failure_callback_error_rolls_back_job_attempt_and_successor(
    transaction_engine: Engine,
) -> None:
    catalogue = build_default_catalogue()
    queue = TransactionalJobQueue(transaction_engine, catalogue)
    original_id = queue.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    ).job_id
    lease = queue.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert lease is not None
    successor_key = f"failure-rollback-proof:{uuid.uuid4()}"

    def enqueue_successor_then_fail(
        _connection: Connection,
        repository: JobRepository,
        _disposition: FailureDisposition,
    ) -> None:
        repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=uuid.uuid4()),
            deduplication_key=successor_key,
        )
        raise RuntimeError("failure mutation failed")

    with pytest.raises(RuntimeError, match="failure mutation failed"):
        queue.fail(
            lease,
            JobFailure(code="dependency_timeout"),
            on_failure=enqueue_successor_then_fail,
        )

    with transaction_engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(Job).where(Job.deduplication_key == successor_key)
            ).scalar_one()
            == 0
        )
        job_state = connection.execute(
            select(Job.status, Job.lease_token).where(Job.id == original_id)
        ).one()
        assert job_state == ("running", lease.lease_token)
        attempt_state = connection.execute(
            select(JobAttempt.status).where(JobAttempt.id == lease.attempt_id)
        ).scalar_one()
        assert attempt_state == "running"
        connection.execute(
            update(Job)
            .where(Job.id == original_id)
            .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )

    queue.recover_expired()
    assert queue.cancel_queued(original_id)


@pytest.mark.postgres
def test_failure_callback_commits_with_database_retry_time(
    transaction_engine: Engine,
) -> None:
    queue = TransactionalJobQueue(
        transaction_engine,
        build_default_catalogue(),
        random_value=lambda: 0.0,
    )
    original_id = queue.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    ).job_id
    lease = queue.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert lease is not None
    successor_key = f"failure-commit-proof:{uuid.uuid4()}"
    callback_dispositions: list[FailureDisposition] = []
    successor_ids: list[uuid.UUID] = []

    def record_failure(
        _connection: Connection,
        repository: JobRepository,
        disposition: FailureDisposition,
    ) -> None:
        callback_dispositions.append(disposition)
        successor_ids.append(
            repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
                deduplication_key=successor_key,
            ).job_id
        )

    disposition = queue.fail(
        lease,
        JobFailure(code="dependency_timeout"),
        on_failure=record_failure,
    )

    assert callback_dispositions == [disposition]
    assert disposition.status == "queued"
    assert disposition.retry_at is not None
    assert len(successor_ids) == 1
    with transaction_engine.connect() as connection:
        persisted = connection.execute(
            select(Job.status, Job.run_after).where(Job.id == original_id)
        ).one()
        assert persisted == ("queued", disposition.retry_at)
        assert (
            connection.execute(select(Job.id).where(Job.id == successor_ids[0])).scalar_one()
            == successor_ids[0]
        )

    assert queue.cancel_queued(original_id)
    assert queue.cancel_queued(successor_ids[0])


@pytest.mark.postgres
def test_stale_lease_never_invokes_failure_callback(transaction_engine: Engine) -> None:
    queue = TransactionalJobQueue(transaction_engine, build_default_catalogue())
    original_id = queue.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    ).job_id
    stale_lease = queue.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
    assert stale_lease is not None
    successor_key = f"stale-callback-proof:{uuid.uuid4()}"
    callbacks = 0

    with transaction_engine.begin() as connection:
        connection.execute(
            update(Job)
            .where(Job.id == original_id)
            .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )
    assert queue.recover_expired().retried == 1

    def forbidden_callback(
        _connection: Connection,
        repository: JobRepository,
        _disposition: FailureDisposition,
    ) -> None:
        nonlocal callbacks
        callbacks += 1
        repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=uuid.uuid4()),
            deduplication_key=successor_key,
        )

    with pytest.raises(LostLease):
        queue.fail(
            stale_lease,
            JobFailure(code="dependency_timeout"),
            on_failure=forbidden_callback,
        )

    assert callbacks == 0
    with transaction_engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(Job).where(Job.deduplication_key == successor_key)
            ).scalar_one()
            == 0
        )
    assert queue.cancel_queued(original_id)
