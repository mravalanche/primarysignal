"""SQL-shape and boundary tests for the job repository."""

import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

from primary_signal.jobs.contracts import JobFailure, PollFeedV1
from primary_signal.jobs.registry import build_default_registry
from primary_signal.jobs.repository import JobLease, JobRepository, LostLease


def _repository(connection: MagicMock) -> JobRepository:
    registry = build_default_registry(
        poll_feed=lambda payload: None, retrieve_article=lambda payload: None
    )
    return JobRepository(cast(Connection, connection), registry, random_value=lambda: 0.5)


def _sql(statement: ClauseElement) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split())


def _lease() -> JobLease:
    return JobLease(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_number=2,
        lease_token=uuid.uuid4(),
        worker_id=f"processor:{uuid.uuid4()}",
        lease_expires_at=datetime.now(UTC),
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
    )


def _result(*, scalar: object = None, rowcount: int = 1) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.rowcount = rowcount
    return result


def test_claim_query_uses_database_clock_order_and_skip_locked() -> None:
    connection = MagicMock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = None
    repository = _repository(connection)

    assert repository.claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}") is None
    sql = _sql(connection.execute.call_args.args[0])

    assert "jobs.run_after <= clock_timestamp()" in sql
    assert "jobs.attempt_count < primary_signal.jobs.max_attempts" in sql
    assert "ORDER BY primary_signal.jobs.priority DESC" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_claim_rejects_unknown_queue_and_nonopaque_worker_id() -> None:
    repository = _repository(MagicMock())

    with pytest.raises(ValueError, match="queue is not registered"):
        repository.claim(queue="other", worker_id=f"processor:{uuid.uuid4()}")
    with pytest.raises(ValidationError):
        repository.claim(queue="ingestion", worker_id="host.example")


def test_enqueue_uses_registered_queue_and_validated_json() -> None:
    connection = MagicMock()
    repository = _repository(connection)
    feed_id = uuid.uuid4()

    result = repository.enqueue(
        job_type="feeds.poll", payload_version=1, payload={"feed_id": str(feed_id)}
    )

    assert isinstance(result.job_id, uuid.UUID)
    assert result.created
    statement = connection.execute.call_args.args[0]
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["queue"] == "ingestion"
    assert params["payload"] == {"feed_id": str(feed_id)}
    assert params["status"] == "queued"


def test_enqueue_rejects_naive_run_after() -> None:
    repository = _repository(MagicMock())

    with pytest.raises(ValueError, match="include a timezone"):
        repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload={"feed_id": str(uuid.uuid4())},
            run_after=datetime.now(),
        )


def test_heartbeat_contains_complete_fence_and_expiry_guard() -> None:
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = None
    repository = _repository(connection)
    lease = _lease()

    with pytest.raises(LostLease, match="cannot be renewed"):
        repository.heartbeat(lease)

    sql = _sql(connection.execute.call_args.args[0])
    assert "jobs.id =" in sql
    assert "jobs.status =" in sql
    assert "jobs.lease_token =" in sql
    assert "jobs.worker_id =" in sql
    assert "jobs.attempt_count =" in sql
    assert "jobs.lease_expires_at > clock_timestamp()" in sql


def test_success_does_not_finalize_attempt_after_lost_fence() -> None:
    connection = MagicMock()
    connection.execute.return_value.rowcount = 0
    repository = _repository(connection)

    with pytest.raises(LostLease, match="completion fence"):
        repository.succeed(_lease())

    assert connection.execute.call_count == 1


def test_only_queued_jobs_can_be_cancelled() -> None:
    connection = MagicMock()
    connection.execute.return_value.rowcount = 1
    repository = _repository(connection)

    assert repository.cancel_queued(uuid.uuid4())
    sql = _sql(connection.execute.call_args.args[0])
    assert "jobs.status =" in sql
    assert "cancelled" in connection.execute.call_args.args[0].compile().params.values()


def test_enqueue_rejects_out_of_range_fields() -> None:
    repository = _repository(MagicMock())
    payload = PollFeedV1(feed_id=uuid.uuid4())

    with pytest.raises(ValueError, match="priority"):
        repository.enqueue(job_type="feeds.poll", payload_version=1, payload=payload, priority=101)
    with pytest.raises(ValueError, match="max attempts"):
        repository.enqueue(
            job_type="feeds.poll", payload_version=1, payload=payload, max_attempts=0
        )
    with pytest.raises(ValueError, match="deduplication key"):
        repository.enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=payload,
            deduplication_key="x" * 257,
        )


def test_enqueue_reports_new_and_existing_deduplicated_jobs() -> None:
    inserted_id = uuid.uuid4()
    inserted_connection = MagicMock()
    inserted_connection.execute.return_value.scalar_one_or_none.return_value = inserted_id
    inserted = _repository(inserted_connection).enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
        deduplication_key="feed:one",
    )
    assert inserted.job_id == inserted_id
    assert inserted.created

    existing_id = uuid.uuid4()
    existing_connection = MagicMock()
    existing_connection.execute.side_effect = [_result(), _result(scalar=existing_id)]
    reused = _repository(existing_connection).enqueue(
        job_type="feeds.poll",
        payload_version=1,
        payload=PollFeedV1(feed_id=uuid.uuid4()),
        deduplication_key="feed:one",
    )
    assert reused.job_id == existing_id
    assert not reused.created


def test_enqueue_stops_after_repeated_deduplication_owner_changes() -> None:
    connection = MagicMock()
    connection.execute.side_effect = [_result() for _ in range(6)]

    with pytest.raises(RuntimeError, match="owner changed repeatedly"):
        _repository(connection).enqueue(
            job_type="feeds.poll",
            payload_version=1,
            payload=PollFeedV1(feed_id=uuid.uuid4()),
            deduplication_key="feed:unstable",
        )


def test_claim_validates_lease_duration() -> None:
    with pytest.raises(ValueError, match="lease duration"):
        _repository(MagicMock()).claim(
            queue="ingestion", worker_id=f"processor:{uuid.uuid4()}", lease_seconds=0
        )


def test_claim_creates_a_fenced_attempt() -> None:
    connection = MagicMock()
    job_id = uuid.uuid4()
    expiry = datetime.now(UTC)
    candidate = MagicMock()
    candidate.mappings.return_value.one_or_none.return_value = {
        "id": job_id,
        "job_type": "feeds.poll",
        "payload_version": 1,
        "payload": {"feed_id": str(uuid.uuid4())},
        "attempt_count": 1,
    }
    connection.execute.side_effect = [candidate, _result(scalar=expiry), _result()]

    lease = _repository(connection).claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")

    assert lease is not None
    assert lease.job_id == job_id
    assert lease.attempt_number == 2
    assert lease.lease_expires_at == expiry
    attempt_params = connection.execute.call_args_list[2].args[0].compile().params
    assert attempt_params["job_id"] == job_id
    assert attempt_params["attempt_number"] == 2


@pytest.mark.parametrize(
    "row",
    [
        {"job_type": "unknown.type", "payload_version": 1, "payload": {}},
        {
            "job_type": "feeds.poll",
            "payload_version": 1,
            "payload": {"feed_id": "not-a-uuid"},
        },
        {
            "job_type": "articles.retrieve",
            "payload_version": 1,
            "payload": {
                "article_id": str(uuid.uuid4()),
                "article_url_id": str(uuid.uuid4()),
            },
        },
    ],
)
def test_claim_rejects_invalid_stored_contract(row: dict[str, object]) -> None:
    connection = MagicMock()
    candidate = MagicMock()
    candidate.mappings.return_value.one_or_none.return_value = {
        "id": uuid.uuid4(),
        "attempt_count": 0,
        **row,
    }
    connection.execute.side_effect = [candidate, _result()]

    assert (
        _repository(connection).claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")
        is None
    )
    reject_params = connection.execute.call_args_list[1].args[0].compile().params
    assert "invalid_job_contract" in reject_params.values()


def test_claim_raises_when_candidate_changes_before_update() -> None:
    connection = MagicMock()
    candidate = MagicMock()
    candidate.mappings.return_value.one_or_none.return_value = {
        "id": uuid.uuid4(),
        "job_type": "feeds.poll",
        "payload_version": 1,
        "payload": {"feed_id": str(uuid.uuid4())},
        "attempt_count": 0,
    }
    connection.execute.side_effect = [candidate, _result()]

    with pytest.raises(LostLease, match="being claimed"):
        _repository(connection).claim(queue="ingestion", worker_id=f"processor:{uuid.uuid4()}")


def test_heartbeat_returns_the_database_expiry_and_validates_duration() -> None:
    expiry = datetime.now(UTC)
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = expiry
    repository = _repository(connection)

    assert repository.heartbeat(_lease()) == expiry
    with pytest.raises(ValueError, match="lease duration"):
        repository.heartbeat(_lease(), lease_seconds=3601)


def test_success_finalizes_job_and_matching_attempt() -> None:
    connection = MagicMock()
    connection.execute.side_effect = [_result(), _result()]
    repository = _repository(connection)

    repository.succeed(_lease())

    assert connection.execute.call_count == 2
    job_sql = _sql(connection.execute.call_args_list[0].args[0])
    assert "jobs.lease_expires_at > clock_timestamp()" in job_sql
    attempt_params = connection.execute.call_args_list[1].args[0].compile().params
    assert "succeeded" in attempt_params.values()


def test_success_raises_when_attempt_fence_is_lost() -> None:
    connection = MagicMock()
    connection.execute.side_effect = [_result(), _result(rowcount=0)]

    with pytest.raises(LostLease, match="attempt fence"):
        _repository(connection).succeed(_lease())


def test_retryable_failure_requeues_and_finalizes_attempt() -> None:
    connection = MagicMock()
    connection.execute.side_effect = [_result(scalar=5), _result(), _result()]

    status = _repository(connection).fail(_lease(), JobFailure(code="dependency_timeout"))

    assert status == "queued"
    job_params = connection.execute.call_args_list[1].args[0].compile().params
    attempt_params = connection.execute.call_args_list[2].args[0].compile().params
    assert "queued" in job_params.values()
    assert "retry" in attempt_params.values()
    assert "dependency_timeout" in attempt_params.values()


@pytest.mark.parametrize(
    ("failure", "maximum"),
    [(JobFailure(code="bad_payload"), None), (JobFailure(code="dependency_timeout"), 2)],
)
def test_failure_becomes_dead_when_permanent_or_attempts_exhausted(
    failure: JobFailure, maximum: int | None
) -> None:
    connection = MagicMock()
    if maximum is None:
        connection.execute.side_effect = [_result(), _result()]
    else:
        connection.execute.side_effect = [_result(scalar=maximum), _result(), _result()]

    assert _repository(connection).fail(_lease(), failure) == "dead"


def test_failure_raises_for_lost_job_and_attempt_fences() -> None:
    lost_job = MagicMock()
    lost_job.execute.side_effect = [_result(rowcount=0)]
    with pytest.raises(LostLease, match="failure fence"):
        _repository(lost_job).fail(_lease(), JobFailure(code="bad_payload"))

    lost_attempt = MagicMock()
    lost_attempt.execute.side_effect = [_result(), _result(rowcount=0)]
    with pytest.raises(LostLease, match="attempt fence"):
        _repository(lost_attempt).fail(_lease(), JobFailure(code="bad_payload"))

    missing_max = MagicMock()
    missing_max.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(LostLease, match="failure fence"):
        _repository(missing_max).fail(_lease(), JobFailure(code="dependency_timeout"))


def test_cancellation_reports_when_no_queued_job_matched() -> None:
    connection = MagicMock()
    connection.execute.return_value.rowcount = 0
    assert not _repository(connection).cancel_queued(uuid.uuid4())


def test_recovery_requeues_known_jobs_and_kills_unknown_or_exhausted_jobs() -> None:
    worker = f"processor:{uuid.uuid4()}"
    rows = [
        {
            "id": uuid.uuid4(),
            "job_type": "feeds.poll",
            "payload_version": 1,
            "attempt_count": 1,
            "max_attempts": 5,
            "worker_id": worker,
            "lease_token": uuid.uuid4(),
        },
        {
            "id": uuid.uuid4(),
            "job_type": "feeds.poll",
            "payload_version": 1,
            "attempt_count": 5,
            "max_attempts": 5,
            "worker_id": worker,
            "lease_token": uuid.uuid4(),
        },
        {
            "id": uuid.uuid4(),
            "job_type": "removed.type",
            "payload_version": 1,
            "attempt_count": 1,
            "max_attempts": 5,
            "worker_id": worker,
            "lease_token": uuid.uuid4(),
        },
    ]
    selection = MagicMock()
    selection.mappings.return_value = rows
    connection = MagicMock()
    connection.execute.side_effect = [
        selection,
        _result(),
        _result(),
        _result(),
        _result(),
        _result(),
        _result(),
    ]

    summary = _repository(connection).recover_expired()

    assert summary.retried == 1
    assert summary.dead == 2


def test_recovery_validates_limit_and_detects_lost_fences() -> None:
    repository = _repository(MagicMock())
    with pytest.raises(ValueError, match="recovery batch"):
        repository.recover_expired(limit=0)

    row = {
        "id": uuid.uuid4(),
        "job_type": "feeds.poll",
        "payload_version": 1,
        "attempt_count": 1,
        "max_attempts": 5,
        "worker_id": f"processor:{uuid.uuid4()}",
        "lease_token": uuid.uuid4(),
    }
    selection = MagicMock()
    selection.mappings.return_value = [row]
    lost_job = MagicMock()
    lost_job.execute.side_effect = [selection, _result(rowcount=0)]
    with pytest.raises(LostLease, match="expired job fence"):
        _repository(lost_job).recover_expired()

    lost_attempt = MagicMock()
    lost_attempt.execute.side_effect = [selection, _result(), _result(rowcount=0)]
    with pytest.raises(LostLease, match="expired attempt fence"):
        _repository(lost_attempt).recover_expired()
