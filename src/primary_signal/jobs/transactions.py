"""Short, engine-owned transaction boundaries for queue operations."""

import random
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime

from sqlalchemy import Connection, Engine

from primary_signal.jobs.catalogue import JobCatalogue
from primary_signal.jobs.contracts import JobFailure, JobPayload
from primary_signal.jobs.repository import (
    EnqueueResult,
    FailureDisposition,
    JobLease,
    JobRepository,
    RecoverySummary,
)

type RandomValue = Callable[[], float]
type RepositoryFactory = Callable[[Connection, JobCatalogue, RandomValue], JobRepository]
type PreparedDatabaseCallback = Callable[[Connection, JobRepository], None]
type PreparedFailureCallback = Callable[[Connection, JobRepository, FailureDisposition], None]


def _repository_factory(
    connection: Connection,
    catalogue: JobCatalogue,
    random_value: RandomValue,
) -> JobRepository:
    return JobRepository(connection, catalogue, random_value=random_value)


class TransactionalJobQueue:
    """Run each queue operation in its own short database transaction.

    Completion callbacks are for already-prepared, bounded database mutations
    only. They run after fenced finalisation but inside the same transaction,
    receive the current connection and repository, and may enqueue successor
    jobs atomically. Any callback failure rolls everything back. They must not
    perform handler or network work.
    """

    def __init__(
        self,
        engine: Engine,
        catalogue: JobCatalogue,
        *,
        repository_factory: RepositoryFactory = _repository_factory,
        random_value: RandomValue = random.random,
    ) -> None:
        self._engine = engine
        self._catalogue = catalogue
        self._repository_factory = repository_factory
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
        with self._engine.begin() as connection:
            return self._repository(connection).enqueue(
                job_type=job_type,
                payload_version=payload_version,
                payload=payload,
                priority=priority,
                max_attempts=max_attempts,
                deduplication_key=deduplication_key,
                run_after=run_after,
            )

    def claim(
        self,
        *,
        queue: str,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> JobLease | None:
        with self._engine.begin() as connection:
            return self._repository(connection).claim(
                queue=queue,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    def heartbeat(self, lease: JobLease, *, lease_seconds: int = 120) -> datetime:
        with self._engine.begin() as connection:
            return self._repository(connection).heartbeat(lease, lease_seconds=lease_seconds)

    def succeed(
        self,
        lease: JobLease,
        *,
        on_success: PreparedDatabaseCallback | None = None,
    ) -> None:
        with self._engine.begin() as connection:
            repository = self._repository(connection)
            repository.succeed(lease)
            if on_success is not None:
                on_success(connection, repository)

    def fail(
        self,
        lease: JobLease,
        failure: JobFailure,
        *,
        on_failure: PreparedFailureCallback | None = None,
    ) -> FailureDisposition:
        with self._engine.begin() as connection:
            repository = self._repository(connection)
            disposition = repository.fail(lease, failure)
            if on_failure is not None:
                on_failure(connection, repository, disposition)
            return disposition

    def recover_expired(self, *, limit: int = 100) -> RecoverySummary:
        with self._engine.begin() as connection:
            return self._repository(connection).recover_expired(limit=limit)

    def cancel_queued(self, job_id: uuid.UUID) -> bool:
        with self._engine.begin() as connection:
            return self._repository(connection).cancel_queued(job_id)

    def _repository(self, connection: Connection) -> JobRepository:
        return self._repository_factory(connection, self._catalogue, self._random_value)
