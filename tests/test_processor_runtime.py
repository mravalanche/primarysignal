"""Bounded processor execution and lease fencing."""

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from primary_signal.config import ProcessorSettings
from primary_signal.entrypoints.processor import ProcessorRuntime, retry_delay
from primary_signal.jobs import (
    JobFailure,
    JobHandlerBinding,
    JobHandlers,
    JobLease,
    JobProcessingError,
    PollFeedV1,
    TransactionalJobQueue,
    build_default_catalogue,
)
from primary_signal.jobs.repository import FailureDisposition, LostLease


def lease() -> JobLease:
    return JobLease(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_number=1,
        lease_token=uuid.uuid4(),
        worker_id=f"processor:{uuid.uuid4()}",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    )


def queue_with_job(job: JobLease | None) -> MagicMock:
    queue = MagicMock(spec=TransactionalJobQueue)
    queue.claim.return_value = job
    queue.fail.return_value = FailureDisposition(status="dead", retry_at=None)
    return queue


def handlers(handler: object) -> JobHandlers:
    return JobHandlers(
        build_default_catalogue(),
        (JobHandlerBinding("feeds.poll", 1, cast(MagicMock, handler)),),
    )


def test_once_recovers_and_runs_one_job_with_prepared_completion() -> None:
    job = lease()
    queue = queue_with_job(job)
    callback = MagicMock()
    handler = MagicMock(return_value=callback)

    ProcessorRuntime(
        cast(TransactionalJobQueue, queue),
        handlers(handler),
        ProcessorSettings(recovery_batch_size=7, lease_seconds=60, heartbeat_interval_seconds=10),
        threading.Event(),
    ).run(once=True)

    queue.recover_expired.assert_called_once_with(limit=7)
    queue.claim.assert_called_once()
    assert queue.claim.call_args.kwargs["queue"] == "ingestion"
    assert queue.claim.call_args.kwargs["lease_seconds"] == 60
    handler.assert_called_once_with(job)
    queue.succeed.assert_called_once_with(job, on_success=callback)
    queue.fail.assert_not_called()


def test_expected_failure_uses_stable_code_and_prepared_failure_callback() -> None:
    job = lease()
    queue = queue_with_job(job)
    callback = MagicMock()

    def handler(_lease: JobLease) -> None:
        raise JobProcessingError("invalid_feed", on_failure=callback)

    ProcessorRuntime(
        cast(TransactionalJobQueue, queue),
        handlers(handler),
        ProcessorSettings(),
        threading.Event(),
    ).run(once=True)

    queue.fail.assert_called_once_with(job, JobFailure(code="invalid_feed"), on_failure=callback)
    queue.succeed.assert_not_called()


def test_unexpected_handler_error_never_persists_or_logs_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = lease()
    queue = queue_with_job(job)

    def handler(_lease: JobLease) -> None:
        raise RuntimeError("private upstream URL and credentials")

    with caplog.at_level(logging.WARNING):
        ProcessorRuntime(
            cast(TransactionalJobQueue, queue),
            handlers(handler),
            ProcessorSettings(),
            threading.Event(),
        ).run(once=True)

    queue.fail.assert_called_once_with(job, JobFailure(code="handler_error"), on_failure=None)
    assert "private upstream" not in caplog.text
    fields = cast(dict[str, object], caplog.records[-1].__dict__["event_fields"])
    assert fields["error_code"] == "handler_error"


def test_long_handler_that_loses_lease_cannot_finalize_stale_work() -> None:
    job = lease()
    queue = queue_with_job(job)
    heartbeat_attempted = threading.Event()

    def lose_lease(_job: JobLease, *, lease_seconds: int) -> None:
        assert lease_seconds == 5
        heartbeat_attempted.set()
        raise LostLease("stale lease")

    def handler(_lease: JobLease) -> None:
        assert heartbeat_attempted.wait(2)

    queue.heartbeat.side_effect = lose_lease
    ProcessorRuntime(
        cast(TransactionalJobQueue, queue),
        handlers(handler),
        ProcessorSettings(lease_seconds=5, heartbeat_interval_seconds=0.1),
        threading.Event(),
    ).run(once=True)

    queue.heartbeat.assert_called_once()
    queue.succeed.assert_not_called()
    queue.fail.assert_not_called()


def test_processor_settings_and_retry_bounds() -> None:
    settings = ProcessorSettings(retry_initial_seconds=2, retry_max_seconds=5)
    assert retry_delay(settings, 1, lambda: 0) == 1
    assert retry_delay(settings, 2, lambda: 1) == 4
    assert retry_delay(settings, 3, lambda: 0.5) == 3.75
    with pytest.raises(ValueError, match="positive"):
        retry_delay(settings, 0, lambda: 0.5)
    with pytest.raises(ValueError, match="heartbeat"):
        ProcessorSettings(lease_seconds=5, heartbeat_interval_seconds=3)
    with pytest.raises(ValueError, match="retry initial"):
        ProcessorSettings(retry_initial_seconds=10, retry_max_seconds=5)


class _DatabaseFault(Exception):
    sqlstate = "40001"


class _StopAfterTwoWaits:
    def __init__(self) -> None:
        self.waits: list[float | None] = []

    def is_set(self) -> bool:
        return len(self.waits) >= 2

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.is_set()


def test_transient_database_failure_retries_then_idles() -> None:
    queue = queue_with_job(None)
    queue.claim.side_effect = [DBAPIError(None, None, _DatabaseFault()), None]
    stop_event = _StopAfterTwoWaits()

    def handler(_lease: JobLease) -> None:
        return None

    ProcessorRuntime(
        cast(TransactionalJobQueue, queue),
        handlers(handler),
        ProcessorSettings(retry_initial_seconds=2, poll_interval_seconds=5),
        cast(threading.Event, stop_event),
        random_value=lambda: 0,
    ).run()

    assert queue.recover_expired.call_count == 2
    assert queue.claim.call_count == 2
    assert stop_event.waits == [1, 5]
