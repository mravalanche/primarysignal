"""Job diagnostics validate filters before touching the database."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection

from primary_signal.jobs.health import (
    JOB_STATUSES,
    HealthReport,
    read_health,
    validate_limit,
    validate_queue,
    validate_status,
)

NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.mark.parametrize("value", [0, 501, True, False, 1.5, "10", None])
def test_limit_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="limit must be an integer"):
        validate_limit(value)


@pytest.mark.parametrize("value", [1, 100, 500])
def test_limit_accepts_bounds(value: int) -> None:
    assert validate_limit(value) == value


@pytest.mark.parametrize("value", ["", "A", "two queues", "a/b", "a" * 65, "é", True, 1])
def test_queue_rejects_invalid_names(value: object) -> None:
    with pytest.raises(ValueError, match="queue must be a machine name"):
        validate_queue(value)


@pytest.mark.parametrize("value", [None, "ingestion", "unknown.queue-name_1", "a" * 64])
def test_queue_accepts_unknown_valid_machine_names(value: str | None) -> None:
    assert validate_queue(value) == value


@pytest.mark.parametrize("value", [None, *JOB_STATUSES])
def test_status_accepts_only_stored_states(value: str | None) -> None:
    assert validate_status(value) == value


@pytest.mark.parametrize("value", ["retry", "lease_expired", "unknown", "Queued", True, 1, ""])
def test_status_rejects_attempt_states_and_unknown_values(value: object) -> None:
    with pytest.raises(ValueError, match="status must be"):
        validate_status(value)


@pytest.mark.parametrize(
    "options",
    [{"queue": "bad/queue"}, {"status": "retry"}, {"limit": 0}, {"limit": True}],
)
def test_invalid_query_does_not_read_database(options: dict[str, object]) -> None:
    connection = MagicMock(spec=Connection)
    with pytest.raises(ValueError):
        read_health(connection, **options)  # type: ignore[arg-type]
    connection.execute.assert_not_called()


def test_empty_health_report_has_explicit_bounds_and_sample_time() -> None:
    assert HealthReport(NOW, [], [], False, False).as_dict() == {
        "as_of": NOW.isoformat(),
        "queues": [],
        "jobs": [],
        "queues_truncated": False,
        "jobs_truncated": False,
    }
