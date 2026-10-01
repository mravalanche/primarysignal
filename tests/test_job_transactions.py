"""Tests for queue transaction ownership and atomic completion."""

import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, call

import pytest
from sqlalchemy import Connection, Engine

from primary_signal.jobs.contracts import JobFailure, PollFeedV1
from primary_signal.jobs.registry import build_default_registry
from primary_signal.jobs.repository import (
    EnqueueResult,
    JobLease,
    JobRepository,
    LostLease,
)
from primary_signal.jobs.transactions import TransactionalJobQueue


def _lease() -> JobLease:
    return JobLease(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_number=1,
        lease_token=uuid.uuid4(),
        worker_id=f"processor:{uuid.uuid4()}",
        lease_expires_at=datetime.now(UTC),
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    )


def _queue() -> tuple[TransactionalJobQueue, MagicMock, MagicMock, MagicMock, MagicMock]:
    engine = MagicMock(spec=Engine)
    transaction = MagicMock()
    connection = MagicMock(spec=Connection)
    transaction.__enter__.return_value = connection
    engine.begin.return_value = transaction
    repository = MagicMock(spec=JobRepository)
    factory = MagicMock(return_value=repository)
    registry = build_default_registry(
        poll_feed=lambda payload: None,
        retrieve_article=lambda payload: None,
    )
    queue = TransactionalJobQueue(
        cast(Engine, engine),
        registry,
        repository_factory=factory,
        random_value=lambda: 0.25,
    )
    return queue, engine, transaction, connection, repository


def _assert_transaction(
    engine: MagicMock,
    transaction: MagicMock,
    connection: MagicMock,
) -> None:
    engine.begin.assert_called_once_with()
    transaction.__enter__.assert_called_once_with()
    transaction.__exit__.assert_called_once_with(None, None, None)
    assert connection is transaction.__enter__.return_value


def test_enqueue_owns_transaction_and_passes_all_options() -> None:
    queue, engine, transaction, connection, repository = _queue()
    payload = PollFeedV1(feed_id=uuid.uuid4())
    expected = EnqueueResult(job_id=uuid.uuid4(), created=True)
    repository.enqueue.return_value = expected
    scheduled = datetime.now(UTC)

    result = queue.enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=payload,
        priority=3,
        max_attempts=4,
        deduplication_key="feed:example",
        run_after=scheduled,
    )

    assert result == expected
    repository.enqueue.assert_called_once_with(
        job_type="feeds.poll",
        payload_version=1,
        payload=payload,
        priority=3,
        max_attempts=4,
        deduplication_key="feed:example",
        run_after=scheduled,
    )
    _assert_transaction(engine, transaction, connection)


def test_claim_heartbeat_fail_recover_and_cancel_each_own_a_transaction() -> None:
    lease = _lease()
    failure = JobFailure(code="upstream_timeout")
    job_id = uuid.uuid4()

    operations = (
        (
            "claim",
            {"queue": "ingestion", "worker_id": lease.worker_id, "lease_seconds": 90},
            call(queue="ingestion", worker_id=lease.worker_id, lease_seconds=90),
        ),
        (
            "heartbeat",
            {"lease": lease, "lease_seconds": 45},
            call(lease, lease_seconds=45),
        ),
        ("fail", {"lease": lease, "failure": failure}, call(lease, failure)),
        ("recover_expired", {"limit": 12}, call(limit=12)),
        ("cancel_queued", {"job_id": job_id}, call(job_id)),
    )
    for method_name, arguments, expected_call in operations:
        queue, engine, transaction, connection, repository = _queue()
        method = getattr(queue, method_name)

        method(**arguments)

        repository_method = getattr(repository, method_name)
        assert repository_method.call_args == expected_call
        _assert_transaction(engine, transaction, connection)


def test_success_callback_and_completion_share_connection_and_repository() -> None:
    queue, engine, transaction, connection, repository = _queue()
    lease = _lease()
    events: list[str] = []

    def record_enqueue(**kwargs: object) -> None:
        events.append("enqueue")

    def record_success(supplied_lease: JobLease) -> None:
        events.append("succeed")

    repository.enqueue.side_effect = record_enqueue
    repository.succeed.side_effect = record_success

    def prepare(current_connection: Connection, current_repository: JobRepository) -> None:
        assert current_connection is connection
        assert current_repository is repository
        events.append("domain-write")
        current_repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=uuid.uuid4()),
        )

    queue.succeed(lease, on_success=prepare)

    assert events == ["succeed", "domain-write", "enqueue"]
    repository.succeed.assert_called_once_with(lease)
    _assert_transaction(engine, transaction, connection)


def test_success_lost_lease_escapes_before_prepared_changes() -> None:
    queue, engine, transaction, _connection, repository = _queue()
    lease = _lease()
    prepared = MagicMock()
    lost_lease = LostLease("completion fence did not match")
    repository.succeed.side_effect = lost_lease

    with pytest.raises(LostLease) as raised:
        queue.succeed(lease, on_success=prepared)

    assert raised.value is lost_lease
    prepared.assert_not_called()
    exit_args = transaction.__exit__.call_args.args
    assert exit_args[:2] == (LostLease, lost_lease)
    assert exit_args[2] is not None
    engine.begin.assert_called_once_with()


def test_prepared_change_error_escapes_without_attempting_completion() -> None:
    queue, engine, transaction, _connection, repository = _queue()
    error = RuntimeError("database mutation failed")

    def fail_preparation(
        current_connection: Connection,
        current_repository: JobRepository,
    ) -> None:
        raise error

    with pytest.raises(RuntimeError) as raised:
        queue.succeed(_lease(), on_success=fail_preparation)

    assert raised.value is error
    repository.succeed.assert_called_once()
    exit_args = transaction.__exit__.call_args.args
    assert exit_args[:2] == (RuntimeError, error)
    assert exit_args[2] is not None
    engine.begin.assert_called_once_with()


def test_repository_error_propagates_out_of_the_transaction() -> None:
    queue, engine, transaction, _connection, repository = _queue()
    error = RuntimeError("database unavailable")
    repository.recover_expired.side_effect = error

    with pytest.raises(RuntimeError) as raised:
        queue.recover_expired()

    assert raised.value is error
    exit_args = transaction.__exit__.call_args.args
    assert exit_args[:2] == (RuntimeError, error)
    assert exit_args[2] is not None
    engine.begin.assert_called_once_with()
