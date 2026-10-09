"""Bounded, metadata-only source and feed health for a local operator."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Connection, Table, func, select

from primary_signal.sources.models import Feed, Source

_sources = cast(Table, Source.__table__)
_feeds = cast(Table, Feed.__table__)
MAX_ITEMS = 500


def validate_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("limit must be an integer between 1 and 500")
    return limit


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def feed_status(
    *,
    enabled: bool,
    source_enabled: bool,
    last_success_at: datetime | None,
    consecutive_failures: int,
    next_poll_at: datetime | None,
    poll_interval_seconds: int,
    now: datetime,
) -> str:
    """Stale means failures, no successful overdue poll, or a success over two intervals old."""
    if not enabled or not source_enabled:
        return "disabled"
    if consecutive_failures > 0:
        return "stale"
    if last_success_at is None:
        return "stale" if next_poll_at is None or next_poll_at <= now else "healthy"
    if last_success_at < now - timedelta(seconds=2 * poll_interval_seconds):
        return "stale"
    return "healthy"


@dataclass(frozen=True, slots=True)
class HealthReport:
    as_of: datetime
    sources: list[dict[str, Any]]
    feeds: list[dict[str, Any]]
    sources_truncated: bool
    feeds_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "sources": self.sources,
            "feeds": self.feeds,
            "sources_truncated": self.sources_truncated,
            "feeds_truncated": self.feeds_truncated,
        }


def read_health(connection: Connection, *, limit: int = 100) -> HealthReport:
    """Run two bounded, ordered SELECTs without retrieving URLs or error detail."""
    limit = validate_limit(limit)
    now = connection.execute(select(func.current_timestamp())).scalar_one()
    source_rows = (
        connection.execute(
            select(_sources.c.source_key, _sources.c.name, _sources.c.enabled)
            .order_by(_sources.c.source_key)
            .limit(limit + 1)
        )
        .mappings()
        .all()
    )
    feed_rows = (
        connection.execute(
            select(
                _feeds.c.id,
                _sources.c.source_key,
                _sources.c.enabled.label("source_enabled"),
                _feeds.c.name,
                _feeds.c.enabled,
                _feeds.c.last_attempt_at,
                _feeds.c.last_success_at,
                _feeds.c.consecutive_failures,
                _feeds.c.next_poll_at,
                _feeds.c.poll_interval_seconds,
            )
            .join(_sources, _sources.c.id == _feeds.c.source_id)
            .order_by(_sources.c.source_key, _feeds.c.name, _feeds.c.id)
            .limit(limit + 1)
        )
        .mappings()
        .all()
    )
    sources = [
        {"key": row["source_key"], "name": row["name"], "enabled": row["enabled"]}
        for row in source_rows[:limit]
    ]
    feeds = [
        {
            "id": str(row["id"]),
            "source_key": row["source_key"],
            "name": row["name"],
            "enabled": row["enabled"],
            "last_attempt_at": _timestamp(row["last_attempt_at"]),
            "last_success_at": _timestamp(row["last_success_at"]),
            "consecutive_failures": row["consecutive_failures"],
            "next_due_at": _timestamp(row["next_poll_at"]),
            "status": feed_status(
                enabled=row["enabled"],
                source_enabled=row["source_enabled"],
                last_success_at=row["last_success_at"],
                consecutive_failures=row["consecutive_failures"],
                next_poll_at=row["next_poll_at"],
                poll_interval_seconds=row["poll_interval_seconds"],
                now=now,
            ),
        }
        for row in feed_rows[:limit]
    ]
    return HealthReport(now, sources, feeds, len(source_rows) > limit, len(feed_rows) > limit)
