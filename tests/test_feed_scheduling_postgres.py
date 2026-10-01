"""PostgreSQL integration proofs for due-feed scheduling."""

import os
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, select, text, update

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import JobPayload
from primary_signal.jobs.models import Job
from primary_signal.jobs.repository import EnqueueResult, JobRepository
from primary_signal.sources.models import Feed, Source
from primary_signal.sources.scheduling import TransactionalFeedScheduler


@pytest.fixture
def scheduling_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Migrate an explicitly disposable database and provide its admin engine."""

    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected_role:
        pytest.skip(
            "set PRIMARY_SIGNAL_TEST_DATABASE_URL and "
            "PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE for PostgreSQL integration tests"
        )

    engine = create_engine(url)
    with engine.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()")).scalar_one()
        assert str(database_name).endswith("_test")
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    command.upgrade(config, "head")
    # Preserve unrelated fixtures in the shared disposable database while
    # ensuring only feeds created by this test can be selected as due.
    with engine.begin() as connection:
        previous_poll_times = connection.execute(select(Feed.id, Feed.next_poll_at)).all()
        connection.execute(update(Feed).values(next_poll_at=None))
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            source_ids = list(
                connection.execute(
                    select(Source.id).where(Source.source_key.like("schedule-test-%"))
                ).scalars()
            )
            if source_ids:
                feed_ids = list(
                    connection.execute(
                        select(Feed.id).where(Feed.source_id.in_(source_ids))
                    ).scalars()
                )
                if feed_ids:
                    connection.execute(
                        delete(Job).where(
                            Job.deduplication_key.in_([f"feed:{feed_id}" for feed_id in feed_ids])
                        )
                    )
                    connection.execute(delete(Feed).where(Feed.id.in_(feed_ids)))
                connection.execute(delete(Source).where(Source.id.in_(source_ids)))
            for feed_id, next_poll_at in previous_poll_times:
                connection.execute(
                    update(Feed).where(Feed.id == feed_id).values(next_poll_at=next_poll_at)
                )
        engine.dispose()


def _add_feed(
    engine: Engine,
    *,
    next_poll_at: datetime | None,
    source_enabled: bool = True,
    feed_enabled: bool = True,
    poll_interval_seconds: int = 900,
    feed_id: uuid.UUID | None = None,
) -> uuid.UUID:
    identity = uuid.uuid4()
    source_id = uuid.uuid7()
    selected_feed_id = feed_id or uuid.uuid7()
    with engine.begin() as connection:
        connection.execute(
            insert(Source).values(
                id=source_id,
                source_key=f"schedule-test-{identity.hex}",
                name="Synthetic source",
                homepage_url="https://public.example/",
                enabled=source_enabled,
            )
        )
        connection.execute(
            insert(Feed).values(
                id=selected_feed_id,
                source_id=source_id,
                name="Synthetic feed",
                configured_url=f"https://public.example/{identity}",
                normalized_url=f"https://public.example/{identity}",
                url_hash=identity.hex * 2,
                url_normalization_version=1,
                enabled=feed_enabled,
                poll_interval_seconds=poll_interval_seconds,
                next_poll_at=next_poll_at,
            )
        )
    return selected_feed_id


def _scheduled_feed_ids(engine: Engine, feed_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    keys = {f"feed:{feed_id}" for feed_id in feed_ids}
    with engine.connect() as connection:
        payloads = connection.execute(
            select(Job.payload).where(Job.deduplication_key.in_(keys))
        ).scalars()
        return {uuid.UUID(str(payload["feed_id"])) for payload in payloads}


@pytest.mark.postgres
def test_due_scheduler_orders_filters_and_advances_from_one_database_time(
    scheduling_engine: Engine,
) -> None:
    now = datetime.now(UTC)
    ordered_ids = sorted((uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))
    due_ids = {
        _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=2), feed_id=feed_id)
        for feed_id in ordered_ids
    }
    _add_feed(scheduling_engine, next_poll_at=None)
    _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=2), source_enabled=False)
    _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=2), feed_enabled=False)
    scheduler = TransactionalFeedScheduler(scheduling_engine, build_default_catalogue())

    summary = scheduler.schedule_due(limit=2)

    assert (summary.selected, summary.enqueued, summary.already_active) == (2, 2, 0)
    assert _scheduled_feed_ids(scheduling_engine, due_ids) == set(ordered_ids[:2])
    with scheduling_engine.connect() as connection:
        scheduled_times = connection.execute(
            select(Feed.next_poll_at, Feed.updated_at)
            .where(Feed.id.in_(ordered_ids[:2]))
            .order_by(Feed.id)
        ).all()
    assert len({next_poll for next_poll, _updated in scheduled_times}) == 1
    assert all(
        next_poll == updated + timedelta(minutes=15) for next_poll, updated in scheduled_times
    )


@pytest.mark.postgres
def test_locked_earliest_feed_is_skipped(scheduling_engine: Engine) -> None:
    now = datetime.now(UTC)
    first_id = _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=3))
    second_id = _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=2))

    with scheduling_engine.connect() as locker:
        transaction = locker.begin()
        locked_id = locker.execute(
            select(Feed.id).where(Feed.id == first_id).with_for_update(of=Feed)
        ).scalar_one()
        assert locked_id == first_id

        summary = TransactionalFeedScheduler(
            scheduling_engine, build_default_catalogue()
        ).schedule_due(limit=1)
        transaction.rollback()

    assert summary.selected == 1
    assert _scheduled_feed_ids(scheduling_engine, {first_id, second_id}) == {second_id}


@pytest.mark.postgres
def test_concurrent_schedulers_partition_due_feeds(scheduling_engine: Engine) -> None:
    now = datetime.now(UTC)
    feed_ids = {
        _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=1)) for _ in range(6)
    }

    def schedule() -> tuple[int, int]:
        summary = TransactionalFeedScheduler(
            scheduling_engine, build_default_catalogue()
        ).schedule_due(limit=3)
        return summary.selected, summary.enqueued

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(schedule) for _ in range(2)]
        summaries = [future.result() for future in futures]

    assert sum(selected for selected, _enqueued in summaries) == 6
    assert sum(enqueued for _selected, enqueued in summaries) == 6
    assert _scheduled_feed_ids(scheduling_engine, feed_ids) == feed_ids


@pytest.mark.postgres
def test_existing_active_job_still_advances_feed(scheduling_engine: Engine) -> None:
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    feed_id = _add_feed(scheduling_engine, next_poll_at=due_at, poll_interval_seconds=120)
    scheduler = TransactionalFeedScheduler(scheduling_engine, build_default_catalogue())
    assert scheduler.schedule_due().enqueued == 1
    with scheduling_engine.begin() as connection:
        connection.execute(update(Feed).where(Feed.id == feed_id).values(next_poll_at=due_at))

    summary = scheduler.schedule_due()

    assert summary.selected == 1
    assert summary.enqueued == 0
    assert summary.already_active == 1
    with scheduling_engine.connect() as connection:
        next_poll_at = connection.execute(
            select(Feed.next_poll_at).where(Feed.id == feed_id)
        ).scalar_one()
    assert next_poll_at is not None
    assert next_poll_at > due_at


@pytest.mark.postgres
def test_enqueue_failure_rolls_back_jobs_and_feed_advances(
    scheduling_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    feed_ids = {
        _add_feed(scheduling_engine, next_poll_at=now - timedelta(minutes=1)) for _ in range(2)
    }

    class FailingRepository(JobRepository):
        calls = 0

        def enqueue(
            self,
            *,
            job_type: str,
            payload_version: int,
            payload: Mapping[str, object] | JobPayload,
            priority: int = 0,
            max_attempts: int = 5,
            deduplication_key: str | None = None,
            run_after: datetime | None = None,
        ) -> EnqueueResult:
            type(self).calls += 1
            if type(self).calls == 2:
                raise RuntimeError("synthetic enqueue failure")
            return super().enqueue(
                job_type=job_type,
                payload_version=payload_version,
                payload=payload,
                priority=priority,
                max_attempts=max_attempts,
                deduplication_key=deduplication_key,
                run_after=run_after,
            )

    monkeypatch.setattr("primary_signal.sources.scheduling.JobRepository", FailingRepository)

    with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
        TransactionalFeedScheduler(scheduling_engine, build_default_catalogue()).schedule_due(
            limit=2
        )

    assert _scheduled_feed_ids(scheduling_engine, feed_ids) == set()
    with scheduling_engine.connect() as connection:
        poll_times = set(
            connection.execute(select(Feed.next_poll_at).where(Feed.id.in_(feed_ids))).scalars()
        )
    assert poll_times == {now - timedelta(minutes=1)}
