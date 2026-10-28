"""Narrow, audited source state transitions through a database function."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Connection, text

from primary_signal.sources.repository import validate_source_key

MAX_REASON_LENGTH = 500
REASON_TRIM_CHARACTERS = (
    " \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def validate_reason(value: object) -> str:
    """Reject raw C0/C1 controls, then trim the same explicit Unicode set as SQL."""

    if not isinstance(value, str) or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
    ):
        raise ValueError("reason must be text without control characters")
    value = value.strip(REASON_TRIM_CHARACTERS)
    if not 1 <= len(value) <= MAX_REASON_LENGTH:
        raise ValueError("reason must contain between 1 and 500 characters after trimming spaces")
    return value


@dataclass(frozen=True, slots=True)
class SourceControlResult:
    source_key: str
    enabled: bool
    changed: bool
    updated_at: datetime
    event_id: uuid.UUID | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "enabled": self.enabled,
            "changed": self.changed,
            "updated_at": self.updated_at.isoformat(),
            "event_id": str(self.event_id) if self.event_id is not None else None,
        }


def set_source_enabled(
    connection: Connection, source_key: str, *, enabled: bool, reason: str
) -> SourceControlResult:
    """Prepare one transition inside the caller's transaction, without direct writes."""

    source_key = validate_source_key(source_key)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    reason = validate_reason(reason)
    row = (
        connection.execute(
            text(
                "SELECT source_key, enabled, changed, updated_at, event_id "
                "FROM primary_signal.set_source_enabled(:source_key, :enabled, :reason)"
            ),
            {"source_key": source_key, "enabled": enabled, "reason": reason},
        )
        .mappings()
        .one()
    )
    return SourceControlResult(
        source_key=cast(str, row["source_key"]),
        enabled=cast(bool, row["enabled"]),
        changed=cast(bool, row["changed"]),
        updated_at=cast(datetime, row["updated_at"]),
        event_id=cast(uuid.UUID | None, row["event_id"]),
    )
