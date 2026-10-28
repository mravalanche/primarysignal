"""The source transition wrapper validates input and returns safe metadata."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection

from primary_signal.sources.control import (
    REASON_TRIM_CHARACTERS,
    SourceControlResult,
    set_source_enabled,
    validate_reason,
)
from primary_signal.sources.repository import validate_source_key

NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, True, "", "   ", "\u00a0", "\u3000", "x" * 501])
def test_reason_rejects_nontext_blank_and_oversize_values(value: object) -> None:
    with pytest.raises(ValueError, match="reason must"):
        validate_reason(value)


@pytest.mark.parametrize("codepoint", [0, 9, 10, 13, 31, 127, 128, 133, 159])
def test_reason_rejects_raw_controls_before_trimming(codepoint: int) -> None:
    with pytest.raises(ValueError, match="without control characters"):
        validate_reason(chr(codepoint) + "review source" + chr(codepoint))


def test_reason_uses_explicit_unicode_trim_set_and_keeps_internal_spaces() -> None:
    assert validate_reason(REASON_TRIM_CHARACTERS + "review  source" + REASON_TRIM_CHARACTERS) == (
        "review  source"
    )
    assert validate_reason("  " + "x" * 500 + "  ") == "x" * 500


@pytest.mark.parametrize("value", ["", "Upper", "bad_key", "-bad", "bad-", "x" * 101, "valid\n"])
def test_source_key_reuses_exact_repository_rule(value: str) -> None:
    with pytest.raises(ValueError, match="source key must"):
        validate_source_key(value)


def test_source_key_preserves_existing_numeric_and_length_bounds() -> None:
    assert validate_source_key("1-authority-source") == "1-authority-source"
    assert validate_source_key("x" * 100) == "x" * 100


@pytest.mark.parametrize(
    "options",
    [
        {"source_key": "bad/key", "enabled": False, "reason": "review"},
        {"source_key": "example-source", "enabled": 1, "reason": "review"},
        {"source_key": "example-source", "enabled": False, "reason": "\u00a0"},
    ],
)
def test_invalid_transition_does_not_touch_database(options: dict[str, object]) -> None:
    connection = MagicMock(spec=Connection)
    with pytest.raises(ValueError):
        set_source_enabled(connection, **options)  # type: ignore[arg-type]
    connection.execute.assert_not_called()


@pytest.mark.parametrize("changed", [True, False])
def test_transition_uses_one_bound_function_call_and_excludes_audit_reason(changed: bool) -> None:
    connection = MagicMock(spec=Connection)
    event_id = uuid.uuid4() if changed else None
    connection.execute.return_value.mappings.return_value.one.return_value = {
        "source_key": "example-source",
        "enabled": False,
        "changed": changed,
        "updated_at": NOW,
        "event_id": event_id,
    }
    reason = "O'Brien requested a pause; SELECT private_audit_marker"
    result = set_source_enabled(
        connection, "example-source", enabled=False, reason="\u00a0" + reason + "\u3000"
    )
    connection.execute.assert_called_once()
    statement, parameters = connection.execute.call_args.args
    assert str(statement) == (
        "SELECT source_key, enabled, changed, updated_at, event_id "
        "FROM primary_signal.set_source_enabled(:source_key, :enabled, :reason)"
    )
    assert parameters == {"source_key": "example-source", "enabled": False, "reason": reason}
    assert reason not in str(statement)
    assert result == SourceControlResult("example-source", False, changed, NOW, event_id)
    assert result.as_dict() == {
        "source_key": "example-source",
        "enabled": False,
        "changed": changed,
        "updated_at": NOW.isoformat(),
        "event_id": str(event_id) if changed else None,
    }
    assert "private_audit_marker" not in repr(result)
