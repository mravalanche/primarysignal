"""Unit tests for bounded feed scheduling."""

import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects import postgresql

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.repository import EnqueueResult, JobRepository
from primary_signal.sources.scheduling import (
    FeedScheduleInvariantError,
    FeedScheduleRepository,
    ScheduleSummary,
    TransactionalFeedScheduler,
)


def _repository() -> tuple[FeedScheduleRepository, MagicMock]:
    connection = MagicMock(spec=Connection)
    return (
        FeedScheduleRepository(
            cast(Connection, connection),
            build_default_catalogue(),
            random_value=lambda: 0.25,
        ),
        connection,
    )


@pytest.mark.parametrize("limit", [0, 501, True, 1.5, "1"])
def test_schedule_due_rejects_invalid_limits_before_query(limit: object) -> None:
    scheduler, connection = _repository()

    with pytest.raises(ValueError, match="integer between 1 and 500"):
        scheduler.schedule_due(limit=cast(int, limit))

    connection.execute.assert_not_called()


def test_schedule_due_uses_one_clock_and_locked_stable_bounded_query() -> None:
    scheduler, connection = _repository()
    database_now = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    first_result = MagicMock()
    first_result.scalar_one.return_value = database_now
    second_result = MagicMock()
    second_result.mappings.return_value.all.return_value = []
    connection.execute.side_effect = [first_result, second_result]

    assert scheduler.schedule_due(limit=37) == ScheduleSummary(0, 0, 0)

    clock_statement = connection.execute.call_args_list[0].args[0]
    due_statement = connection.execute.call_args_list[1].args[0]
    clock_sql = str(
        clock_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    due_sql = str(
        due_statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert clock_sql == "SELECT clock_timestamp() AS clock_timestamp_1"
    assert "JOIN primary_signal.sources" in due_sql
    assert "primary_signal.sources.enabled IS true" in due_sql
    assert "primary_signal.feeds.enabled IS true" in due_sql
    assert "primary_signal.feeds.next_poll_at IS NOT NULL" in due_sql
    assert "ORDER BY primary_signal.feeds.next_poll_at, primary_signal.feeds.id" in due_sql
    assert "LIMIT 37" in due_sql
    assert "FOR UPDATE OF feeds SKIP LOCKED" in due_sql


def test_schedule_due_enqueues_ids_and_advances_created_or_active_feeds() -> None:
    scheduler, connection = _repository()
    database_now = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    first_feed = uuid.uuid4()
    second_feed = uuid.uuid4()
    clock_result = MagicMock()
    clock_result.scalar_one.return_value = database_now
    due_result = MagicMock()
    due_result.mappings.return_value.all.return_value = [
        {"id": first_feed, "poll_interval_seconds": 60},
        {"id": second_feed, "poll_interval_seconds": 900},
    ]
    first_update = MagicMock(rowcount=1)
    second_update = MagicMock(rowcount=1)
    connection.execute.side_effect = [clock_result, due_result, first_update, second_update]
    queue = MagicMock(spec=JobRepository)
    queue.enqueue.side_effect = [
        EnqueueResult(job_id=uuid.uuid4(), created=True),
        EnqueueResult(job_id=uuid.uuid4(), created=False),
    ]

    with patch("primary_signal.sources.scheduling.JobRepository", return_value=queue) as factory:
        summary = scheduler.schedule_due(limit=2)

    assert summary == ScheduleSummary(selected=2, enqueued=1, already_active=1)
    factory.assert_called_once()
    assert factory.call_args.args[0] is connection
    assert factory.call_args.args[1] is not None
    assert factory.call_args.kwargs["random_value"]() == 0.25
    assert queue.enqueue.call_args_list == [
        call(
            job_type="feeds.poll",
            payload_version=1,
            payload=queue.enqueue.call_args_list[0].kwargs["payload"],
            deduplication_key=f"feed:{first_feed}",
            run_after=database_now,
        ),
        call(
            job_type="feeds.poll",
            payload_version=1,
            payload=queue.enqueue.call_args_list[1].kwargs["payload"],
            deduplication_key=f"feed:{second_feed}",
            run_after=database_now,
        ),
    ]
    assert queue.enqueue.call_args_list[0].kwargs["payload"].feed_id == first_feed
    assert queue.enqueue.call_args_list[1].kwargs["payload"].feed_id == second_feed
    update_parameters = [
        invocation.args[0].compile(dialect=postgresql.dialect()).params
        for invocation in connection.execute.call_args_list[2:]
    ]
    assert update_parameters[0]["next_poll_at"] == datetime(2026, 10, 1, 9, 31, tzinfo=UTC)
    assert update_parameters[0]["updated_at"] == database_now
    assert update_parameters[1]["next_poll_at"] == datetime(2026, 10, 1, 9, 45, tzinfo=UTC)
    assert update_parameters[1]["updated_at"] == database_now


def test_schedule_due_rejects_a_missing_locked_feed_update() -> None:
    scheduler, connection = _repository()
    database_now = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    feed_id = uuid.uuid4()
    clock_result = MagicMock()
    clock_result.scalar_one.return_value = database_now
    due_result = MagicMock()
    due_result.mappings.return_value.all.return_value = [
        {"id": feed_id, "poll_interval_seconds": 60}
    ]
    update_result = MagicMock(rowcount=0)
    connection.execute.side_effect = [clock_result, due_result, update_result]
    queue = MagicMock(spec=JobRepository)
    queue.enqueue.return_value = EnqueueResult(job_id=uuid.uuid4(), created=True)

    with (
        patch("primary_signal.sources.scheduling.JobRepository", return_value=queue),
        pytest.raises(FeedScheduleInvariantError, match="locked feed was not advanced"),
    ):
        scheduler.schedule_due()


def test_transactional_scheduler_owns_commit_and_rollback_boundaries() -> None:
    engine = MagicMock(spec=Engine)
    transaction = MagicMock()
    connection = MagicMock(spec=Connection)
    transaction.__enter__.return_value = connection
    engine.begin.return_value = transaction
    expected = ScheduleSummary(1, 1, 0)

    with patch(
        "primary_signal.sources.scheduling.FeedScheduleRepository.schedule_due",
        return_value=expected,
    ) as schedule_due:
        scheduler = TransactionalFeedScheduler(
            cast(Engine, engine), build_default_catalogue(), random_value=lambda: 0.5
        )
        assert scheduler.schedule_due(limit=12) == expected

    schedule_due.assert_called_once_with(limit=12)
    transaction.__exit__.assert_called_once_with(None, None, None)

    error = RuntimeError("scheduling failed")
    transaction.reset_mock()
    with (
        patch(
            "primary_signal.sources.scheduling.FeedScheduleRepository.schedule_due",
            side_effect=error,
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        scheduler.schedule_due()

    assert raised.value is error
    exit_args = transaction.__exit__.call_args.args
    assert exit_args[:2] == (RuntimeError, error)
    assert exit_args[2] is not None
