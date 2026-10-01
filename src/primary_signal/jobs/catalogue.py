"""Immutable catalogue of durable job contracts."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import TypeAdapter, ValidationError

from primary_signal.jobs.contracts import (
    JobPayload,
    MachineName,
    PollFeedV1,
    QueueName,
    RetrieveArticleV1,
)
from primary_signal.jobs.retry import RetryPolicy


@dataclass(frozen=True, slots=True)
class JobContract[PayloadT: JobPayload]:
    """A versioned payload contract and its queue policy."""

    job_type: str
    payload_version: int
    queue: str
    payload_model: type[PayloadT]
    retry: RetryPolicy


class UnknownJobContract(ValueError):
    """Raised when a type/version pair is not in the catalogue."""


class InvalidJobPayload(ValueError):
    """Raised when a known payload fails strict validation."""


class JobCatalogue:
    """Immutable type/version map containing data definitions, not handlers."""

    def __init__(self, contracts: Iterable[JobContract[Any]]) -> None:
        entries: dict[tuple[str, int], JobContract[Any]] = {}
        queues: dict[str, list[JobContract[Any]]] = {}
        for contract in contracts:
            TypeAdapter(MachineName).validate_python(contract.job_type, strict=True)
            TypeAdapter(QueueName).validate_python(contract.queue, strict=True)
            if contract.payload_version < 1:
                raise ValueError("payload version must be positive")
            key = (contract.job_type, contract.payload_version)
            if key in entries:
                raise ValueError(
                    f"duplicate job contract: {contract.job_type} v{contract.payload_version}"
                )
            entries[key] = contract
            queues.setdefault(contract.queue, []).append(contract)

        self._entries = MappingProxyType(entries)
        self._queues = MappingProxyType(
            {queue: tuple(queue_contracts) for queue, queue_contracts in queues.items()}
        )

    def get(self, job_type: str, payload_version: int) -> JobContract[Any]:
        try:
            return self._entries[(job_type, payload_version)]
        except KeyError as error:
            raise UnknownJobContract(
                f"unsupported job contract: {job_type} v{payload_version}"
            ) from error

    def validate(self, job_type: str, payload_version: int, payload: object) -> JobPayload:
        contract = self.get(job_type, payload_version)
        try:
            # Strict JSON mode accepts JSON UUID strings but rejects loose field
            # coercion.
            encoded = json.dumps(payload, separators=(",", ":"))
            return contract.payload_model.model_validate_json(encoded, strict=True)
        except (TypeError, ValueError, ValidationError) as error:
            raise InvalidJobPayload("job payload does not match its registered contract") from error

    def supports_queue(self, queue: str) -> bool:
        return queue in self._queues

    def contracts_for_queue(self, queue: str) -> tuple[JobContract[Any], ...]:
        """Return the contracts assigned to a known queue."""

        try:
            return self._queues[queue]
        except KeyError as error:
            raise ValueError(f"unknown job queue: {queue}") from error


def build_default_catalogue() -> JobCatalogue:
    """Build the initial contract catalogue without importing process handlers."""

    transient = frozenset({"dependency_timeout", "dependency_unavailable", "rate_limited"})
    return JobCatalogue(
        (
            JobContract(
                job_type="feeds.poll",
                payload_version=1,
                queue="ingestion",
                payload_model=PollFeedV1,
                retry=RetryPolicy(retryable_error_codes=transient),
            ),
            JobContract(
                job_type="articles.retrieve",
                payload_version=1,
                queue="retrieval",
                payload_model=RetrieveArticleV1,
                retry=RetryPolicy(retryable_error_codes=transient),
            ),
        )
    )
