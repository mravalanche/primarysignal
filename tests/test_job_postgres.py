"""PostgreSQL integration tests for durable job queue semantics."""

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, insert, select, text, update

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import JobFailure, PollFeedV1
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.repository import EnqueueResult, JobRepository, LostLease


@pytest.fixture
def job_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
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


def _catalogue():
    return build_default_catalogue()


def _worker_id() -> str:
    return f"processor:{uuid.uuid4()}"


@pytest.mark.postgres
def test_enqueue_deduplicates_active_jobs(job_engine: Engine) -> None:
    key = f"feed:{uuid.uuid4()}"
    payload = PollFeedV1(feed_id=uuid.uuid4())

    with job_engine.connect() as connection:
        transaction = connection.begin()
        repository = JobRepository(connection, _catalogue())
        try:
            first = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=payload,
                deduplication_key=key,
            )
            second = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=payload,
                deduplication_key=key,
            )

            assert first.created
            assert not second.created
            assert second.job_id == first.job_id
            assert (
                connection.execute(
                    select(func.count()).select_from(Job).where(Job.deduplication_key == key)
                ).scalar_one()
                == 1
            )
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_concurrent_enqueues_share_one_active_job(job_engine: Engine) -> None:
    key = f"feed:{uuid.uuid4()}"
    payload = PollFeedV1(feed_id=uuid.uuid4())
    ready = Barrier(2)

    def enqueue_once() -> EnqueueResult:
        with job_engine.begin() as connection:
            ready.wait(timeout=5)
            return JobRepository(connection, _catalogue()).enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=payload,
                deduplication_key=key,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(enqueue_once), executor.submit(enqueue_once))
        results = tuple(future.result(timeout=10) for future in futures)

    assert {result.created for result in results} == {False, True}
    assert len({result.job_id for result in results}) == 1
    with job_engine.begin() as cleanup:
        cleanup.execute(
            text("DELETE FROM primary_signal.jobs WHERE id=:id"),
            {"id": results[0].job_id},
        )


@pytest.mark.postgres
def test_recovery_fences_out_the_stale_worker(job_engine: Engine) -> None:
    with job_engine.connect() as connection:
        transaction = connection.begin()
        repository = JobRepository(connection, _catalogue(), random_value=lambda: 0.5)
        try:
            job_id = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
            ).job_id
            stale_lease = repository.claim(queue="ingestion", worker_id=_worker_id())
            assert stale_lease is not None
            assert stale_lease.job_id == job_id

            connection.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
            )
            with pytest.raises(LostLease, match="completion fence"):
                repository.succeed(stale_lease)
            summary = repository.recover_expired()

            assert summary.retried == 1
            assert summary.dead == 0
            with pytest.raises(LostLease, match="cannot be renewed"):
                repository.heartbeat(stale_lease)
            state = connection.execute(
                select(Job.status, Job.worker_id, Job.lease_token).where(Job.id == job_id)
            ).one()
            assert state == ("queued", None, None)
            attempt = connection.execute(
                select(JobAttempt.status, JobAttempt.error_code).where(
                    JobAttempt.id == stale_lease.attempt_id
                )
            ).one()
            assert attempt == ("lease_expired", "lease_expired")
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_cancel_only_affects_queued_jobs(job_engine: Engine) -> None:
    with job_engine.connect() as connection:
        transaction = connection.begin()
        repository = JobRepository(connection, _catalogue())
        try:
            queued_id = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
            ).job_id
            running_id = repository.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
                priority=1,
            ).job_id
            lease = repository.claim(queue="ingestion", worker_id=_worker_id())
            assert lease is not None
            assert lease.job_id == running_id

            assert repository.cancel_queued(queued_id)
            assert not repository.cancel_queued(running_id)
            status_rows = connection.execute(
                select(Job.id, Job.status).where(Job.id.in_((queued_id, running_id)))
            ).all()
            statuses = {row.id: row.status for row in status_rows}
            assert statuses == {queued_id: "cancelled", running_id: "running"}
        finally:
            transaction.rollback()


@pytest.mark.postgres
def test_claimers_skip_rows_locked_by_another_worker(job_engine: Engine) -> None:
    with job_engine.begin() as setup:
        job_id = (
            JobRepository(setup, _catalogue())
            .enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
            )
            .job_id
        )

    first_connection = job_engine.connect()
    second_connection = job_engine.connect()
    first_transaction = first_connection.begin()
    second_transaction = second_connection.begin()
    try:
        first = JobRepository(first_connection, _catalogue()).claim(
            queue="ingestion", worker_id=_worker_id()
        )
        second = JobRepository(second_connection, _catalogue()).claim(
            queue="ingestion", worker_id=_worker_id()
        )

        assert first is not None
        assert first.job_id == job_id
        assert second is None
    finally:
        second_transaction.rollback()
        first_transaction.rollback()
        second_connection.close()
        first_connection.close()

    with job_engine.begin() as cleanup:
        cleanup.execute(text("DELETE FROM primary_signal.jobs WHERE id=:id"), {"id": job_id})


@pytest.mark.postgres
def test_reclaimed_job_rejects_all_writes_from_the_previous_worker(
    job_engine: Engine,
) -> None:
    catalogue = _catalogue()
    with job_engine.begin() as connection:
        job_id = (
            JobRepository(connection, catalogue)
            .enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
            )
            .job_id
        )

    with job_engine.begin() as connection:
        stale_lease = JobRepository(connection, catalogue).claim(
            queue="ingestion", worker_id=_worker_id()
        )
        assert stale_lease is not None

    with job_engine.begin() as connection:
        connection.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )
        repository = JobRepository(connection, catalogue, random_value=lambda: 0.0)
        assert repository.recover_expired().retried == 1
        connection.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(run_after=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )

    with job_engine.begin() as connection:
        current_lease = JobRepository(connection, catalogue).claim(
            queue="ingestion", worker_id=_worker_id()
        )
        assert current_lease is not None
        assert current_lease.job_id == job_id

    with job_engine.begin() as connection:
        stale_repository = JobRepository(connection, catalogue)
        with pytest.raises(LostLease):
            stale_repository.heartbeat(stale_lease)
        with pytest.raises(LostLease):
            stale_repository.succeed(stale_lease)
        with pytest.raises(LostLease):
            stale_repository.fail(stale_lease, JobFailure(code="bad_payload"))

    with job_engine.begin() as connection:
        row = connection.execute(
            select(Job.worker_id, Job.lease_token, Job.attempt_count).where(Job.id == job_id)
        ).one()
        assert row == (
            current_lease.worker_id,
            current_lease.lease_token,
            current_lease.attempt_number,
        )
        JobRepository(connection, catalogue).succeed(current_lease)


@pytest.mark.postgres
def test_retryable_failures_stop_at_the_attempt_limit(job_engine: Engine) -> None:
    catalogue = _catalogue()
    with job_engine.begin() as connection:
        job_id = (
            JobRepository(connection, catalogue)
            .enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
                max_attempts=2,
            )
            .job_id
        )

    with job_engine.begin() as connection:
        repository = JobRepository(connection, catalogue, random_value=lambda: 0.0)
        first = repository.claim(queue="ingestion", worker_id=_worker_id())
        assert first is not None
        first_failure = repository.fail(first, JobFailure(code="dependency_timeout"))
        assert first_failure.status == "queued"
        assert first_failure.retry_at is not None
        connection.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(run_after=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )

    with job_engine.begin() as connection:
        repository = JobRepository(connection, catalogue, random_value=lambda: 0.0)
        second = repository.claim(queue="ingestion", worker_id=_worker_id())
        assert second is not None
        assert second.attempt_number == 2
        second_failure = repository.fail(second, JobFailure(code="dependency_timeout"))
        assert second_failure.status == "dead"
        assert second_failure.retry_at is None

    with job_engine.connect() as connection:
        assert connection.execute(select(Job.status).where(Job.id == job_id)).scalar_one() == "dead"
        assert connection.execute(
            select(JobAttempt.status)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt_number)
        ).scalars().all() == ["retry", "dead"]


@pytest.mark.postgres
def test_permanent_failure_and_malformed_payload_die_without_retry(
    job_engine: Engine,
) -> None:
    catalogue = _catalogue()
    malformed_id = uuid.uuid7()
    with job_engine.begin() as connection:
        permanent_id = (
            JobRepository(connection, catalogue)
            .enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=uuid.uuid4()),
                priority=1,
            )
            .job_id
        )
        connection.execute(
            insert(Job).values(
                id=malformed_id,
                job_type="feeds.poll",
                payload_version=1,
                payload={"feed_id": "not-a-uuid"},
                queue="ingestion",
                run_after=func.clock_timestamp(),
            )
        )

    with job_engine.begin() as connection:
        repository = JobRepository(connection, catalogue)
        permanent = repository.claim(queue="ingestion", worker_id=_worker_id())
        assert permanent is not None
        assert permanent.job_id == permanent_id
        permanent_failure = repository.fail(permanent, JobFailure(code="bad_payload"))
        assert permanent_failure.status == "dead"
        assert permanent_failure.retry_at is None
        assert repository.claim(queue="ingestion", worker_id=_worker_id()) is None

    with job_engine.connect() as connection:
        rows = connection.execute(
            select(Job.id, Job.status).where(Job.id.in_((permanent_id, malformed_id)))
        ).all()
        states = {row.id: row.status for row in rows}
        assert states == {permanent_id: "dead", malformed_id: "dead"}
