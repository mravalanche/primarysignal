"""Strict, bounded contracts for durable jobs."""

import json
import uuid
from typing import Annotated, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PAYLOAD_BYTES = 8 * 1024
MAX_PAYLOAD_DEPTH = 4
MAX_PAYLOAD_ITEMS = 32
MAX_PAYLOAD_STRING_LENGTH = 512

MachineName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")]
QueueName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
DeduplicationKey = Annotated[str, Field(min_length=1, max_length=256)]
WorkerId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*:[0-9a-f-]{36}$")
]


def _measure(value: object, *, depth: int = 0) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise ValueError("payload is too deeply nested")
    if isinstance(value, str):
        if len(value) > MAX_PAYLOAD_STRING_LENGTH:
            raise ValueError("payload string is too long")
        return
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        if len(mapping) > MAX_PAYLOAD_ITEMS:
            raise ValueError("payload object has too many fields")
        for key, item in mapping.items():
            _measure(key, depth=depth + 1)
            _measure(item, depth=depth + 1)
        return
    if isinstance(value, list):
        items = cast(list[object], value)
        if len(items) > MAX_PAYLOAD_ITEMS:
            raise ValueError("payload list has too many items")
        for item in items:
            _measure(item, depth=depth + 1)


class JobPayload(BaseModel):
    """Base for versioned JSON job payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def payload_is_bounded(self) -> Self:
        value = self.model_dump(mode="json")
        _measure(value)
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds the application size limit")
        return self


class PollFeedV1(JobPayload):
    """Request a poll of one configured feed."""

    feed_id: uuid.UUID


class RetrieveArticleV1(JobPayload):
    """Request retrieval of one known article URL."""

    article_id: uuid.UUID
    article_url_id: uuid.UUID


class JobFailure(BaseModel):
    """A stable failure code supplied by a handler, never a raw exception."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: MachineName
