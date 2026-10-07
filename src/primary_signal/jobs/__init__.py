"""Durable, versioned PostgreSQL work queue."""

from primary_signal.jobs.catalogue import JobCatalogue, JobContract, build_default_catalogue
from primary_signal.jobs.contracts import JobFailure, JobPayload, PollFeedV1, RetrieveArticleV1
from primary_signal.jobs.handlers import JobHandlerBinding, JobHandlers, JobProcessingError
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.jobs.repository import (
    EnqueueResult,
    FailureDisposition,
    JobLease,
    JobRepository,
    LostLease,
    RecoverySummary,
)
from primary_signal.jobs.retry import RetryPolicy
from primary_signal.jobs.transactions import (
    PreparedDatabaseCallback,
    PreparedFailureCallback,
    TransactionalJobQueue,
)

__all__ = [
    "EnqueueResult",
    "FailureDisposition",
    "Job",
    "JobAttempt",
    "JobCatalogue",
    "JobContract",
    "JobFailure",
    "JobHandlerBinding",
    "JobHandlers",
    "JobLease",
    "JobPayload",
    "JobProcessingError",
    "JobRepository",
    "LostLease",
    "PollFeedV1",
    "PreparedDatabaseCallback",
    "PreparedFailureCallback",
    "RecoverySummary",
    "RetrieveArticleV1",
    "RetryPolicy",
    "TransactionalJobQueue",
    "build_default_catalogue",
]
