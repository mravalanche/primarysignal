"""PostgreSQL proof of atomic job completion rollback."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, func, select, text, update

from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.models import Job
from primary_signal.jobs.registry import build_default_registry
from primary_signal.jobs.repository import JobRepository
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
    registry = build_default_registry(
        poll_feed=lambda payload: None,
        retrieve_article=lambda payload: None,
    )
    queue = TransactionalJobQueue(transaction_engine, registry)
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
