"""Bounded, metadata-only queue diagnostics for a local operator."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Connection, Table, func, select

from primary_signal.jobs.models import Job

_jobs = cast(Table, Job.__table__)
_QUEUE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
MAX_ITEMS = 500
JOB_STATUSES = ("queued", "running", "dead", "succeeded", "cancelled")


def validate_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ITEMS:
        raise ValueError("limit must be an integer between 1 and 500")
    return value


def validate_queue(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _QUEUE.fullmatch(value):
        raise ValueError("queue must be a machine name of at most 64 characters")
    return value


def validate_status(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in JOB_STATUSES:
        raise ValueError("status must be queued, running, dead, succeeded or cancelled")
    return value


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class HealthReport:
    as_of: datetime
    queues: list[dict[str, Any]]
    jobs: list[dict[str, Any]]
    queues_truncated: bool
    jobs_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "queues": self.queues,
            "jobs": self.jobs,
            "queues_truncated": self.queues_truncated,
            "jobs_truncated": self.jobs_truncated,
        }


def read_health(
    connection: Connection,
    *,
    queue: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> HealthReport:
    """Sample queue totals and recent jobs using one database clock value.

    The status filter affects details only. These separate SELECTs do not
    guarantee an exact snapshot while workers change jobs concurrently.
    """

    queue = validate_queue(queue)
    status = validate_status(status)
    limit = validate_limit(limit)
    now = cast(datetime, connection.execute(select(func.clock_timestamp())).scalar_one())
    queued = _jobs.c.status == "queued"
    running = _jobs.c.status == "running"
    queues_query = (
        select(
            _jobs.c.queue,
            func.count().filter(queued).label("queued"),
            func.count().filter(queued, _jobs.c.run_after <= now).label("due"),
            func.count().filter(queued, _jobs.c.run_after > now).label("delayed"),
            func.count().filter(queued, _jobs.c.attempt_count > 0).label("queued_retries"),
            func.count().filter(running).label("running"),
            func.count().filter(running, _jobs.c.lease_expires_at <= now).label("expired"),
            *(
                func.count().filter(_jobs.c.status == state).label(state)
                for state in ("dead", "succeeded", "cancelled")
            ),
        )
        .group_by(_jobs.c.queue)
        .order_by(_jobs.c.queue)
        .limit(limit + 1)
    )
    jobs_query = (
        select(
            _jobs.c.id,
            _jobs.c.queue,
            _jobs.c.job_type,
            _jobs.c.status,
            _jobs.c.attempt_count,
            _jobs.c.max_attempts,
            _jobs.c.run_after,
            _jobs.c.lease_expires_at,
            _jobs.c.heartbeat_at,
            _jobs.c.created_at,
            _jobs.c.completed_at,
        )
        .order_by(_jobs.c.created_at.desc(), _jobs.c.id.desc())
        .limit(limit + 1)
    )
    if queue is not None:
        queues_query = queues_query.where(_jobs.c.queue == queue)
        jobs_query = jobs_query.where(_jobs.c.queue == queue)
    if status is not None:
        jobs_query = jobs_query.where(_jobs.c.status == status)
    queue_rows = connection.execute(queues_query).mappings().all()
    job_rows = connection.execute(jobs_query).mappings().all()
    queues = [dict(row) for row in queue_rows[:limit]]
    jobs = [
        {
            "id": str(row["id"]),
            "queue": row["queue"],
            "job_type": row["job_type"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "run_after": _timestamp(row["run_after"]),
            "lease_expires_at": _timestamp(row["lease_expires_at"]),
            "heartbeat_at": _timestamp(row["heartbeat_at"]),
            "created_at": _timestamp(row["created_at"]),
            "completed_at": _timestamp(row["completed_at"]),
        }
        for row in job_rows[:limit]
    ]
    return HealthReport(now, queues, jobs, len(queue_rows) > limit, len(job_rows) > limit)
