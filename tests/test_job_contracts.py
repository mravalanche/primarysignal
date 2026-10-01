"""Tests for strict job contracts, registration, and retry policy."""

import uuid
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from primary_signal.jobs.contracts import JobFailure, JobPayload, PollFeedV1
from primary_signal.jobs.registry import (
    InvalidJobPayload,
    JobDefinition,
    JobRegistry,
    UnknownJobContract,
    build_default_registry,
)
from primary_signal.jobs.retry import RetryPolicy


def _ignore(payload: JobPayload) -> None:
    del payload


def test_default_registry_validates_exact_contract_and_queue() -> None:
    registry = build_default_registry(
        poll_feed=lambda payload: None, retrieve_article=lambda payload: None
    )
    feed_id = uuid.uuid4()

    payload = registry.validate("feeds.poll", 1, {"feed_id": str(feed_id)})

    assert payload == PollFeedV1(feed_id=feed_id)
    assert registry.supports_queue("ingestion")
    assert not registry.supports_queue("arbitrary")
    with pytest.raises(InvalidJobPayload, match="registered contract"):
        registry.validate("feeds.poll", 1, {"feed_id": str(feed_id), "extra": True})
    with pytest.raises(UnknownJobContract, match="unsupported job contract"):
        registry.validate("feeds.poll", 2, {"feed_id": str(feed_id)})


def test_registry_rejects_duplicates_and_invalid_machine_names() -> None:
    definition = JobDefinition(
        job_type="feeds.poll",
        payload_version=1,
        queue="ingestion",
        payload_model=PollFeedV1,
        handler=_ignore,
        retry=RetryPolicy(),
    )
    with pytest.raises(ValueError, match="duplicate job contract"):
        JobRegistry((definition, definition))

    invalid = JobDefinition(
        job_type="Bad Job",
        payload_version=1,
        queue="ingestion",
        payload_model=PollFeedV1,
        handler=_ignore,
        retry=RetryPolicy(),
    )
    with pytest.raises(ValidationError):
        JobRegistry((invalid,))


class _BoundedPayload(JobPayload):
    values: list[str]


def test_payload_limits_apply_to_every_contract() -> None:
    with pytest.raises(ValidationError, match="too many items"):
        _BoundedPayload(values=[str(number) for number in range(33)])
    with pytest.raises(ValidationError, match="too long"):
        _BoundedPayload(values=["x" * 513])


def test_failure_accepts_only_a_stable_machine_code() -> None:
    assert JobFailure(code="dependency_timeout")
    with pytest.raises(ValidationError):
        JobFailure.model_validate(
            {"code": "dependency_timeout", "detail": "https://private.example/path?token=value"}
        )
    with pytest.raises(ValidationError):
        JobFailure(code="UPPERCASE")


@pytest.mark.parametrize(
    ("attempt", "sample", "expected"),
    [(1, 0.0, 5.0), (1, 0.5, 7.5), (2, 1.0, 20.0), (8, 1.0, 50.0)],
)
def test_retry_delay_is_exponential_jittered_and_capped(
    attempt: int, sample: float, expected: float
) -> None:
    policy = RetryPolicy(base_delay_seconds=10, maximum_delay_seconds=50)

    assert policy.delay_seconds(attempt, random_value=lambda: sample) == expected


def test_retry_only_permits_explicit_error_codes() -> None:
    policy = RetryPolicy(retryable_error_codes=frozenset({"dependency_timeout"}))

    assert policy.permits("dependency_timeout")
    assert not policy.permits("bad_payload")
    with pytest.raises(ValueError, match="random value"):
        policy.delay_seconds(1, random_value=lambda: 1.1)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: RetryPolicy(base_delay_seconds=0), "base retry delay"),
        (lambda: RetryPolicy(base_delay_seconds=float("nan")), "base retry delay"),
        (
            lambda: RetryPolicy(base_delay_seconds=10, maximum_delay_seconds=5),
            "maximum retry delay",
        ),
        (lambda: RetryPolicy(maximum_delay_seconds=float("inf")), "maximum retry delay"),
        (lambda: RetryPolicy(maximum_delay_seconds=86_401), "maximum retry delay"),
    ],
)
def test_retry_policy_rejects_unbounded_configuration(
    factory: Callable[[], RetryPolicy], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_retry_delay_requires_a_positive_attempt() -> None:
    with pytest.raises(ValueError, match="attempt number"):
        RetryPolicy().delay_seconds(0, random_value=lambda: 0.5)
