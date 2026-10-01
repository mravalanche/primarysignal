"""Synchronous PostgreSQL operations for the durable job queue."""

import random
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import Connection, Select, Table, and_, bindparam, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from primary_signal.jobs.contracts import JobFailure, JobPayload, WorkerId
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.registry import (
    InvalidJobPayload,
    JobDefinition,
    JobRegistry,
    UnknownJobContract,
)

_jobs = cast(Table, Job.__table__)
_attempts = cast(Table, JobAttempt.__table__)


class LostLease(RuntimeError):
    """The job is no longer owned by the supplied execution fence."""


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    lease_token: uuid.UUID
    worker_id: str
    lease_expires_at: datetime
    job_type: str
    payload_version: int
    payload: JobPayload


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    retried: int = 0
    dead: int = 0


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: uuid.UUID
    created: bool


def _lease_interval(seconds: int) -> Any:
    # PostgreSQL's make_interval keeps the duration a bound value rather than
    # interpolating SQL text.
    return func.make_interval(0, 0, 0, 0, 0, 0, literal(seconds))


def _fence(lease: JobLease, *, require_unexpired: bool = False) -> Any:
    predicates = [
        _jobs.c.id == lease.job_id,
        _jobs.c.status == "running",
        _jobs.c.lease_token == lease.lease_token,
        _jobs.c.worker_id == lease.worker_id,
        _jobs.c.attempt_count == lease.attempt_number,
    ]
    if require_unexpired:
        predicates.append(_jobs.c.lease_expires_at > func.clock_timestamp())
    return and_(*predicates)


class JobRepository:
    """Queue persistence; callers provide the surrounding short transaction."""

    def __init__(
        self,
        connection: Connection,
        registry: JobRegistry,
        *,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._connection = connection
        self._registry = registry
        self._random_value = random_value

    def enqueue(
        self,
        *,
        job_type: str,
        payload_version: int,
        payload: Mapping[str, object] | JobPayload,
        priority: int = 0,
        max_attempts: int = 5,
        deduplication_key: str | None = None,
        run_after: datetime | None = None,
    ) -> EnqueueResult:
        definition = self._registry.get(job_type, payload_version)
        raw_payload: object = (
            payload.model_dump(mode="json") if isinstance(payload, JobPayload) else dict(payload)
        )
        validated = self._registry.validate(job_type, payload_version, raw_payload)
        if not -100 <= priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max attempts must be between 1 and 20")
        if deduplication_key is not None and not 1 <= len(deduplication_key) <= 256:
            raise ValueError("deduplication key must be between 1 and 256 characters")
        if run_after is not None and run_after.utcoffset() is None:
            raise ValueError("run_after must include a timezone")

        job_id = uuid.uuid7()
        values = {
            "id": job_id,
            "job_type": job_type,
            "payload_version": payload_version,
            "payload": validated.model_dump(mode="json"),
            "queue": definition.queue,
            "priority": priority,
            "deduplication_key": deduplication_key,
            "status": "queued",
            "run_after": func.clock_timestamp() if run_after is None else run_after,
            "max_attempts": max_attempts,
        }
        statement = insert(_jobs).values(**values)
        if deduplication_key is None:
            self._connection.execute(statement)
            return EnqueueResult(job_id=job_id, created=True)

        statement = statement.on_conflict_do_nothing(
            index_elements=[_jobs.c.queue, _jobs.c.job_type, _jobs.c.deduplication_key],
            index_where=and_(
                _jobs.c.deduplication_key.is_not(None),
                _jobs.c.status.in_(("queued", "running")),
            ),
        ).returning(_jobs.c.id)
        for _ in range(3):
            inserted = self._connection.execute(statement).scalar_one_or_none()
            if inserted is not None:
                return EnqueueResult(job_id=cast(uuid.UUID, inserted), created=True)
            existing = self._connection.execute(
                select(_jobs.c.id).where(
                    _jobs.c.queue == definition.queue,
                    _jobs.c.job_type == job_type,
                    _jobs.c.deduplication_key == deduplication_key,
                    _jobs.c.status.in_(("queued", "running")),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return EnqueueResult(job_id=cast(uuid.UUID, existing), created=False)
        raise RuntimeError("active deduplication owner changed repeatedly")

    def claim(self, *, queue: str, worker_id: str, lease_seconds: int = 120) -> JobLease | None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease duration must be between 1 and 3600 seconds")
        if not self._registry.supports_queue(queue):
            raise ValueError("queue is not registered in this process")
        TypeAdapter(WorkerId).validate_python(worker_id, strict=True)

        candidate = self._claim_candidate(queue)
        row = self._connection.execute(candidate).mappings().one_or_none()
        if row is None:
            return None

        try:
            definition = self._registry.get(row["job_type"], row["payload_version"])
            payload = self._registry.validate(
                row["job_type"], row["payload_version"], row["payload"]
            )
        except UnknownJobContract, InvalidJobPayload:
            self._reject_invalid_candidate(cast(uuid.UUID, row["id"]))
            return None
        if definition.queue != queue:
            self._reject_invalid_candidate(cast(uuid.UUID, row["id"]))
            return None

        job_id = cast(uuid.UUID, row["id"])
        attempt_number = cast(int, row["attempt_count"]) + 1
        attempt_id = uuid.uuid7()
        lease_token = uuid.uuid7()
        lease_expiry = func.clock_timestamp() + _lease_interval(lease_seconds)
        claimed = self._connection.execute(
            update(_jobs)
            .where(_jobs.c.id == job_id, _jobs.c.status == "queued")
            .values(
                status="running",
                attempt_count=attempt_number,
                worker_id=worker_id,
                lease_token=lease_token,
                heartbeat_at=func.clock_timestamp(),
                lease_expires_at=lease_expiry,
                first_started_at=func.coalesce(_jobs.c.first_started_at, func.clock_timestamp()),
                updated_at=func.clock_timestamp(),
            )
            .returning(_jobs.c.lease_expires_at)
        ).scalar_one_or_none()
        if claimed is None:
            raise LostLease("job changed while it was being claimed")
        self._connection.execute(
            insert(_attempts).values(
                id=attempt_id,
                job_id=job_id,
                attempt_number=attempt_number,
                worker_id=worker_id,
                status="running",
                started_at=func.clock_timestamp(),
                initial_lease_expires_at=claimed,
            )
        )
        return JobLease(
            job_id=job_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            lease_token=lease_token,
            worker_id=worker_id,
            lease_expires_at=cast(datetime, claimed),
            job_type=cast(str, row["job_type"]),
            payload_version=cast(int, row["payload_version"]),
            payload=payload,
        )

    def heartbeat(self, lease: JobLease, *, lease_seconds: int = 120) -> datetime:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease duration must be between 1 and 3600 seconds")
        renewed = self._connection.execute(
            update(_jobs)
            .where(_fence(lease, require_unexpired=True))
            .values(
                heartbeat_at=func.clock_timestamp(),
                lease_expires_at=func.clock_timestamp() + _lease_interval(lease_seconds),
                updated_at=func.clock_timestamp(),
            )
            .returning(_jobs.c.lease_expires_at)
        ).scalar_one_or_none()
        if renewed is None:
            raise LostLease("job lease cannot be renewed")
        return cast(datetime, renewed)

    def succeed(self, lease: JobLease) -> None:
        changed = self._connection.execute(
            update(_jobs)
            .where(_fence(lease, require_unexpired=True))
            .values(
                status="succeeded",
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                completed_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
        ).rowcount
        if changed != 1:
            raise LostLease("job completion fence did not match")
        self._finish_attempt(lease, status="succeeded")

    def fail(self, lease: JobLease, failure: JobFailure) -> str:
        definition = self._registry.get(lease.job_type, lease.payload_version)
        retry = definition.retry.permits(
            failure.code
        ) and lease.attempt_number < self._max_attempts(lease)
        status = "queued" if retry else "dead"
        values: dict[str, object] = {
            "status": status,
            "worker_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "last_error_code": failure.code,
            "last_error_detail": None,
            "updated_at": func.clock_timestamp(),
        }
        if retry:
            delay = definition.retry.delay_seconds(
                lease.attempt_number, random_value=self._random_value
            )
            values["run_after"] = func.clock_timestamp() + _lease_interval(max(1, round(delay)))
        else:
            values["completed_at"] = func.clock_timestamp()
        changed = self._connection.execute(
            update(_jobs).where(_fence(lease, require_unexpired=True)).values(**values)
        ).rowcount
        if changed != 1:
            raise LostLease("job failure fence did not match")
        self._finish_attempt(
            lease,
            status="retry" if retry else "dead",
            failure=failure,
        )
        return status

    def cancel_queued(self, job_id: uuid.UUID) -> bool:
        changed = self._connection.execute(
            update(_jobs)
            .where(_jobs.c.id == job_id, _jobs.c.status == "queued")
            .values(
                status="cancelled",
                completed_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
        ).rowcount
        return changed == 1

    def recover_expired(self, *, limit: int = 100) -> RecoverySummary:
        if not 1 <= limit <= 1000:
            raise ValueError("recovery batch must be between 1 and 1000")
        rows = self._connection.execute(
            select(
                _jobs.c.id,
                _jobs.c.job_type,
                _jobs.c.payload_version,
                _jobs.c.attempt_count,
                _jobs.c.max_attempts,
                _jobs.c.worker_id,
                _jobs.c.lease_token,
            )
            .where(
                _jobs.c.status == "running",
                _jobs.c.lease_expires_at <= func.clock_timestamp(),
            )
            .order_by(_jobs.c.lease_expires_at, _jobs.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).mappings()
        retried = 0
        dead = 0
        for row in rows:
            if self._recover_one(row):
                retried += 1
            else:
                dead += 1
        return RecoverySummary(retried=retried, dead=dead)

    def _claim_candidate(self, queue: str) -> Select[tuple[Any, ...]]:
        return (
            select(
                _jobs.c.id,
                _jobs.c.job_type,
                _jobs.c.payload_version,
                _jobs.c.payload,
                _jobs.c.attempt_count,
            )
            .where(
                _jobs.c.queue == bindparam("claim_queue", value=queue),
                _jobs.c.status == "queued",
                _jobs.c.run_after <= func.clock_timestamp(),
                _jobs.c.attempt_count < _jobs.c.max_attempts,
            )
            .order_by(_jobs.c.priority.desc(), _jobs.c.run_after, _jobs.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    def _reject_invalid_candidate(self, job_id: uuid.UUID) -> None:
        self._connection.execute(
            update(_jobs)
            .where(_jobs.c.id == job_id, _jobs.c.status == "queued")
            .values(
                status="dead",
                completed_at=func.clock_timestamp(),
                last_error_code="invalid_job_contract",
                last_error_detail="Stored job does not match a supported contract.",
                updated_at=func.clock_timestamp(),
            )
        )

    def _max_attempts(self, lease: JobLease) -> int:
        value = self._connection.execute(
            select(_jobs.c.max_attempts).where(_fence(lease, require_unexpired=True))
        ).scalar_one_or_none()
        if value is None:
            raise LostLease("job failure fence did not match")
        return cast(int, value)

    def _finish_attempt(
        self,
        lease: JobLease,
        *,
        status: str,
        failure: JobFailure | None = None,
    ) -> None:
        changed = self._connection.execute(
            update(_attempts)
            .where(
                _attempts.c.id == lease.attempt_id,
                _attempts.c.job_id == lease.job_id,
                _attempts.c.attempt_number == lease.attempt_number,
                _attempts.c.worker_id == lease.worker_id,
                _attempts.c.status == "running",
            )
            .values(
                status=status,
                finished_at=func.clock_timestamp(),
                error_code=None if failure is None else failure.code,
                error_detail=None,
            )
        ).rowcount
        if changed != 1:
            raise LostLease("job attempt fence did not match")

    def _recover_one(self, row: RowMapping | Mapping[str, object]) -> bool:
        job_id = cast(uuid.UUID, row["id"])
        attempt_number = cast(int, row["attempt_count"])
        worker_id = cast(str, row["worker_id"])
        lease_token = cast(uuid.UUID, row["lease_token"])
        definition: JobDefinition[Any] | None
        try:
            definition = self._registry.get(
                cast(str, row["job_type"]), cast(int, row["payload_version"])
            )
        except UnknownJobContract:
            definition = None
        retry = definition is not None and attempt_number < cast(int, row["max_attempts"])
        values: dict[str, object] = {
            "status": "queued" if retry else "dead",
            "worker_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "last_error_code": "lease_expired",
            "last_error_detail": "Worker lease expired before completion.",
            "updated_at": func.clock_timestamp(),
        }
        if retry:
            if definition is None:
                raise AssertionError("retry requires a registered job definition")
            delay = definition.retry.delay_seconds(attempt_number, random_value=self._random_value)
            values["run_after"] = func.clock_timestamp() + _lease_interval(max(1, round(delay)))
        else:
            values["completed_at"] = func.clock_timestamp()
        changed = self._connection.execute(
            update(_jobs)
            .where(
                _jobs.c.id == job_id,
                _jobs.c.status == "running",
                _jobs.c.worker_id == worker_id,
                _jobs.c.lease_token == lease_token,
                _jobs.c.attempt_count == attempt_number,
                _jobs.c.lease_expires_at <= func.clock_timestamp(),
            )
            .values(**values)
        ).rowcount
        if changed != 1:
            raise LostLease("expired job fence did not match")
        attempt_changed = self._connection.execute(
            update(_attempts)
            .where(
                _attempts.c.job_id == job_id,
                _attempts.c.attempt_number == attempt_number,
                _attempts.c.worker_id == worker_id,
                _attempts.c.status == "running",
            )
            .values(
                status="lease_expired",
                finished_at=func.clock_timestamp(),
                error_code="lease_expired",
                error_detail="Worker lease expired before completion.",
            )
        ).rowcount
        if attempt_changed != 1:
            raise LostLease("expired attempt fence did not match")
        return retry
