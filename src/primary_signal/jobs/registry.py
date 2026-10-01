"""Static registry for supported job contracts and handlers."""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, TypeVar, cast

from pydantic import TypeAdapter, ValidationError

from primary_signal.jobs.contracts import (
    JobPayload,
    MachineName,
    PollFeedV1,
    QueueName,
    RetrieveArticleV1,
)
from primary_signal.jobs.retry import RetryPolicy

PayloadT = TypeVar("PayloadT", bound=JobPayload)
HandlerPayloadT = TypeVar("HandlerPayloadT", bound=JobPayload, contravariant=True)


class JobHandler(Protocol[HandlerPayloadT]):
    """A handler explicitly wired into the process at startup."""

    def __call__(self, payload: HandlerPayloadT) -> None: ...


@dataclass(frozen=True, slots=True)
class JobDefinition[PayloadT: JobPayload]:
    job_type: str
    payload_version: int
    queue: str
    payload_model: type[PayloadT]
    handler: JobHandler[PayloadT]
    retry: RetryPolicy


class UnknownJobContract(ValueError):
    """Raised when a type/version pair is not registered."""


class InvalidJobPayload(ValueError):
    """Raised when a registered payload fails strict validation."""


class JobRegistry:
    """Immutable type/version map; it never imports handlers by name."""

    def __init__(self, definitions: Iterable[JobDefinition[Any]]) -> None:
        entries: dict[tuple[str, int], JobDefinition[Any]] = {}
        for definition in definitions:
            TypeAdapter(MachineName).validate_python(definition.job_type, strict=True)
            TypeAdapter(QueueName).validate_python(definition.queue, strict=True)
            key = (definition.job_type, definition.payload_version)
            if key in entries:
                raise ValueError(
                    f"duplicate job contract: {definition.job_type} v{definition.payload_version}"
                )
            if definition.payload_version < 1:
                raise ValueError("payload version must be positive")
            entries[key] = definition
        self._entries = MappingProxyType(entries)
        self._queues = frozenset(definition.queue for definition in entries.values())

    def get(self, job_type: str, payload_version: int) -> JobDefinition[Any]:
        try:
            return self._entries[(job_type, payload_version)]
        except KeyError as error:
            raise UnknownJobContract(
                f"unsupported job contract: {job_type} v{payload_version}"
            ) from error

    def validate(self, job_type: str, payload_version: int, payload: object) -> JobPayload:
        definition = self.get(job_type, payload_version)
        try:
            # Strict JSON mode accepts JSON UUID strings but rejects loose field
            # coercion.
            encoded = json.dumps(payload, separators=(",", ":"))
            return definition.payload_model.model_validate_json(encoded, strict=True)
        except (TypeError, ValueError, ValidationError) as error:
            raise InvalidJobPayload("job payload does not match its registered contract") from error

    def supports_queue(self, queue: str) -> bool:
        return queue in self._queues


def build_default_registry(
    *,
    poll_feed: Callable[[PollFeedV1], None],
    retrieve_article: Callable[[RetrieveArticleV1], None],
) -> JobRegistry:
    """Wire the initial contracts to concrete handlers without dynamic imports."""

    transient = frozenset({"dependency_timeout", "dependency_unavailable", "rate_limited"})
    definitions = (
        JobDefinition(
            job_type="feeds.poll",
            payload_version=1,
            queue="ingestion",
            payload_model=PollFeedV1,
            handler=cast(JobHandler[PollFeedV1], poll_feed),
            retry=RetryPolicy(retryable_error_codes=transient),
        ),
        JobDefinition(
            job_type="articles.retrieve",
            payload_version=1,
            queue="retrieval",
            payload_model=RetrieveArticleV1,
            handler=cast(JobHandler[RetrieveArticleV1], retrieve_article),
            retry=RetryPolicy(retryable_error_codes=transient),
        ),
    )
    return JobRegistry(definitions)
