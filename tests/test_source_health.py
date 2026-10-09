"""Source health classification and output bounds."""

from datetime import UTC, datetime, timedelta

import pytest

from primary_signal.sources.health import HealthReport, feed_status, validate_limit

NOW = datetime(2026, 10, 9, tzinfo=UTC)


@pytest.mark.parametrize("value", [0, 501, True, 1.5, "10"])
def test_limit_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_limit(value)


def test_status_rules() -> None:
    def status(**changes: object) -> str:
        values: dict[str, object] = {
            "enabled": True,
            "source_enabled": True,
            "last_success_at": NOW,
            "consecutive_failures": 0,
            "next_poll_at": NOW + timedelta(seconds=900),
            "poll_interval_seconds": 900,
            "now": NOW,
        }
        values.update(changes)
        return feed_status(**values)  # type: ignore[arg-type]

    assert status() == "healthy"
    assert status(enabled=False) == "disabled"
    assert status(source_enabled=False) == "disabled"
    assert status(consecutive_failures=1) == "stale"
    assert status(last_success_at=NOW - timedelta(seconds=1801)) == "stale"
    assert status(last_success_at=None, next_poll_at=NOW) == "stale"
    assert status(last_success_at=None) == "healthy"


def test_empty_report() -> None:
    assert HealthReport(NOW, [], [], False, False).as_dict() == {
        "as_of": NOW.isoformat(),
        "sources": [],
        "feeds": [],
        "sources_truncated": False,
        "feeds_truncated": False,
    }
