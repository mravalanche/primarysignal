"""Concurrency-safe scheduling of due feed polls."""

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import Connection, Engine, Table, func, select, update

from primary_signal.jobs.catalogue import JobCatalogue
from primary_signal.jobs.contracts import PollFeedV1
from primary_signal.jobs.repository import JobRepository
from primary_signal.sources.models import Feed, Source

_feeds = cast(Table, Feed.__table__)
_sources = cast(Table, Source.__table__)

type RandomValue = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ScheduleSummary:
    """Counts from one bounded scheduling pass."""

    selected: int
    enqueued: int
    already_active: int


class FeedScheduleInvariantError(RuntimeError):
    """A locked feed could not be advanced as expected."""


def _validated_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    return value


class FeedScheduleRepository:
    """Schedule due feeds inside a caller-owned transaction."""

    def __init__(
        self,
        connection: Connection,
        catalogue: JobCatalogue,
        *,
        random_value: RandomValue = random.random,
    ) -> None:
        self._connection = connection
        self._catalogue = catalogue
        self._random_value = random_value

    def schedule_due(self, *, limit: int = 100) -> ScheduleSummary:
        limit = _validated_limit(limit)

        database_now = cast(
            datetime,
            self._connection.execute(select(func.clock_timestamp())).scalar_one(),
        )
        due_feeds = (
            self._connection.execute(
                select(
                    _feeds.c.id,
                    _feeds.c.poll_interval_seconds,
                )
                .join(_sources, _sources.c.id == _feeds.c.source_id)
                .where(
                    _sources.c.enabled.is_(True),
                    _feeds.c.enabled.is_(True),
                    _feeds.c.next_poll_at.is_not(None),
                    _feeds.c.next_poll_at <= database_now,
                )
                .order_by(_feeds.c.next_poll_at, _feeds.c.id)
                .limit(limit)
                .with_for_update(of=_feeds, skip_locked=True)
            )
            .mappings()
            .all()
        )

        queue = JobRepository(
            self._connection,
            self._catalogue,
            random_value=self._random_value,
        )
        selected = 0
        enqueued = 0
        for row in due_feeds:
            feed_id = cast(uuid.UUID, row["id"])
            poll_interval = cast(int, row["poll_interval_seconds"])
            result = queue.enqueue(
                job_type="feeds.poll",
                payload_version=1,
                payload=PollFeedV1(feed_id=feed_id),
                deduplication_key=f"feed:{feed_id}",
                run_after=database_now,
            )
            selected += 1
            enqueued += int(result.created)
            update_result = self._connection.execute(
                update(_feeds)
                .where(_feeds.c.id == feed_id)
                .values(
                    next_poll_at=database_now + timedelta(seconds=poll_interval),
                    updated_at=database_now,
                )
            )
            if update_result.rowcount != 1:
                raise FeedScheduleInvariantError("locked feed was not advanced")

        return ScheduleSummary(
            selected=selected,
            enqueued=enqueued,
            already_active=selected - enqueued,
        )


class TransactionalFeedScheduler:
    """Give every bounded scheduling pass its own transaction."""

    def __init__(
        self,
        engine: Engine,
        catalogue: JobCatalogue,
        *,
        random_value: RandomValue = random.random,
    ) -> None:
        self._engine = engine
        self._catalogue = catalogue
        self._random_value = random_value

    def schedule_due(self, *, limit: int = 100) -> ScheduleSummary:
        with self._engine.begin() as connection:
            return FeedScheduleRepository(
                connection,
                self._catalogue,
                random_value=self._random_value,
            ).schedule_due(limit=limit)
