"""Execute-only source decisions and their scheduler serialization boundary."""

import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.models import Job
from primary_signal.jobs.transactions import TransactionalJobQueue
from primary_signal.sources.control import SourceControlResult, set_source_enabled
from primary_signal.sources.models import Feed, Source, SourceStateEvent
from primary_signal.sources.scheduling import FeedScheduleRepository, ScheduleSummary

ROLE = "primary_signal_cap_source_control"
FUNCTION = "primary_signal.set_source_enabled(text,boolean,text)"
CALL = text("SELECT * FROM primary_signal.set_source_enabled(:key,:enabled,:reason)")


@pytest.fixture
def control_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, Engine, Engine]]:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if (
        not url
        or not expected
        or os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE") != "true"
    ):
        pytest.skip("requires an explicitly disposable PostgreSQL database")
    control_url = os.environ.get("PRIMARY_SIGNAL_TEST_SOURCE_CONTROL_DATABASE_URL")
    scheduler_url = os.environ.get("PRIMARY_SIGNAL_TEST_SCHEDULER_DATABASE_URL")
    if not control_url or not scheduler_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires restricted source-control and scheduler login DSNs")
        pytest.skip("set restricted source-control and scheduler login DSNs")
    owner = create_engine(url, hide_parameters=True)
    control = create_engine(control_url, hide_parameters=True)
    scheduler = create_engine(scheduler_url, hide_parameters=True)
    with owner.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        with control.connect() as connection:
            assert (
                connection.execute(text("SELECT current_user")).scalar_one()
                == "source_control_test"
            )
        yield owner, control, scheduler
    finally:
        # Audit rows remain immutable. Disable only this module's synthetic
        # fixtures and cancel only their unclaimed successor jobs.
        with owner.begin() as connection:
            sources = select(Source.id).where(Source.source_key.like("control-test-%"))
            feed_ids = select(Feed.id).where(Feed.source_id.in_(sources))
            connection.execute(update(Source).where(Source.id.in_(sources)).values(enabled=False))
            connection.execute(
                update(Feed).where(Feed.id.in_(feed_ids)).values(enabled=False, next_poll_at=None)
            )
            jobs = (
                connection.execute(
                    select(Job.id).where(
                        Job.status == "queued",
                        Job.deduplication_key.in_(
                            select(func.concat("feed:", Feed.id)).where(Feed.source_id.in_(sources))
                        ),
                    )
                )
                .scalars()
                .all()
            )
        queue = TransactionalJobQueue(owner, build_default_catalogue())
        for job_id in jobs:
            queue.cancel_queued(job_id)
        for engine in (scheduler, control, owner):
            engine.dispose()


def _source(owner: Engine, *, feed: bool = False) -> tuple[uuid.UUID, str, uuid.UUID | None]:
    source_id = uuid.uuid7()
    source_key = f"control-test-{source_id.hex}"
    feed_id = uuid.uuid7() if feed else None
    with owner.begin() as connection:
        connection.execute(
            insert(Source).values(
                id=source_id,
                source_key=source_key,
                name="Synthetic source",
                homepage_url="https://public.example/",
            )
        )
        if feed_id is not None:
            connection.execute(
                insert(Feed).values(
                    id=feed_id,
                    source_id=source_id,
                    name="Synthetic feed",
                    configured_url=f"https://public.example/{feed_id}/feed",
                    normalized_url=f"https://public.example/{feed_id}/feed",
                    url_hash=feed_id.hex * 2,
                    url_normalization_version=1,
                    next_poll_at=datetime(1, 1, 1, tzinfo=UTC),
                    etag='"synthetic"',
                    last_modified="Synthetic modification",
                    consecutive_failures=2,
                )
            )
    return source_id, source_key, feed_id


def _events(owner: Engine, source_id: uuid.UUID) -> list[dict[str, Any]]:
    with owner.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(SourceStateEvent)
                .where(SourceStateEvent.source_id == source_id)
                .order_by(SourceStateEvent.occurred_at, SourceStateEvent.id)
            ).mappings()
        ]


def _wait_for_block(owner: Engine, waiting: int, blocker: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with owner.connect() as connection:
            if (
                blocker
                in connection.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting}
                ).scalar_one()
            ):
                return
        threading.Event().wait(0.01)
    pytest.fail("synthetic source transition did not reach its expected row-lock wait")


@pytest.mark.postgres
def test_source_control_is_audited_idempotent_and_preserves_feeds(
    control_engines: tuple[Engine, Engine, Engine],
) -> None:
    owner, control, _scheduler = control_engines
    source_id, key, feed_id = _source(owner, feed=True)
    with owner.connect() as connection:
        original_source = (
            connection.execute(select(Source.__table__).where(Source.id == source_id))
            .mappings()
            .one()
        )
        original_feed = dict(
            connection.execute(select(Feed.__table__).where(Feed.id == feed_id)).mappings().one()
        )
    with control.begin() as connection:
        unchanged = set_source_enabled(connection, key, enabled=True, reason="Check existing state")
    assert not unchanged.changed and unchanged.event_id is None
    assert unchanged.updated_at == original_source["updated_at"]
    assert _events(owner, source_id) == []
    with control.begin() as connection:
        paused = set_source_enabled(
            connection, key, enabled=False, reason="\u00a0 Pause synthetic source \u3000"
        )
    assert paused.changed and paused.enabled is False and paused.event_id is not None
    with control.begin() as connection:
        repeated = set_source_enabled(connection, key, enabled=False, reason="Already paused")
    assert not repeated.changed and repeated.event_id is None
    assert repeated.updated_at == paused.updated_at
    events = _events(owner, source_id)
    assert len(events) == 1
    assert events[0]["id"] == paused.event_id
    assert events[0]["old_enabled"] is True and events[0]["new_enabled"] is False
    assert events[0]["reason"] == "Pause synthetic source"
    assert events[0]["actor"] == "source_control_test"
    assert events[0]["occurred_at"] == paused.updated_at
    with control.begin() as connection:
        resumed = set_source_enabled(
            connection, key, enabled=True, reason="Resume synthetic source"
        )
    assert resumed.changed and resumed.updated_at >= paused.updated_at
    with owner.connect() as connection:
        assert (
            dict(
                connection.execute(select(Feed.__table__).where(Feed.id == feed_id))
                .mappings()
                .one()
            )
            == original_feed
        )
        current_source = dict(
            connection.execute(select(Source.__table__).where(Source.id == source_id))
            .mappings()
            .one()
        )
    assert {k: v for k, v in current_source.items() if k != "updated_at"} == {
        k: v for k, v in original_source.items() if k != "updated_at"
    }
    assert [(row["old_enabled"], row["new_enabled"]) for row in _events(owner, source_id)] == [
        (True, False),
        (False, True),
    ]
    for statement in (
        "UPDATE primary_signal.source_state_events SET reason='changed' WHERE id=:id",
        "DELETE FROM primary_signal.source_state_events WHERE id=:id",
    ):
        with pytest.raises(DBAPIError, match="immutable history"), owner.begin() as connection:
            connection.execute(text(statement), {"id": paused.event_id})


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("key", "enabled", "reason"),
    [
        (None, False, "Reason"),
        ("", False, "Reason"),
        ("Uppercase", False, "Reason"),
        ("trailing\n", False, "Reason"),
        ("double--hyphen", False, "Reason"),
        ("a" * 101, False, "Reason"),
        ("use-fixture", None, "Reason"),
        ("use-fixture", False, None),
        ("use-fixture", False, ""),
        ("use-fixture", False, "\u00a0\u3000"),
        ("use-fixture", False, "x" * 501),
        ("use-fixture", False, "line\nfeed"),
        ("use-fixture", False, "control\x7f"),
        ("use-fixture", False, "control\x85"),
    ],
)
def test_source_control_sql_rejects_invalid_input_without_mutation(
    control_engines: tuple[Engine, Engine, Engine],
    key: str | None,
    enabled: bool | None,
    reason: str | None,
) -> None:
    owner, control, _scheduler = control_engines
    source_id, source_key, _ = _source(owner)
    with pytest.raises(DBAPIError) as caught, control.begin() as connection:
        connection.execute(
            CALL,
            {
                "key": source_key if key == "use-fixture" else key,
                "enabled": enabled,
                "reason": reason,
            },
        )
    assert isinstance(caught.value.orig, psycopg.Error) and caught.value.orig.sqlstate == "22023"
    assert _events(owner, source_id) == []
    with owner.connect() as connection:
        assert (
            connection.execute(select(Source.enabled).where(Source.id == source_id)).scalar_one()
            is True
        )


@pytest.mark.postgres
def test_source_control_missing_source_and_failed_transaction_leave_no_event(
    control_engines: tuple[Engine, Engine, Engine],
) -> None:
    owner, control, _scheduler = control_engines
    with pytest.raises(DBAPIError) as caught, control.begin() as connection:
        connection.execute(
            CALL,
            {
                "key": f"missing-{uuid.uuid7().hex}",
                "enabled": False,
                "reason": "Synthetic missing source",
            },
        )
    assert isinstance(caught.value.orig, psycopg.Error) and caught.value.orig.sqlstate == "P0002"
    source_id, key, _ = _source(owner)
    with pytest.raises(RuntimeError, match="synthetic rollback"), control.begin() as connection:
        set_source_enabled(connection, key, enabled=False, reason="Will roll back")
        raise RuntimeError("synthetic rollback")
    with owner.connect() as connection:
        assert (
            connection.execute(select(Source.enabled).where(Source.id == source_id)).scalar_one()
            is True
        )
    assert _events(owner, source_id) == []


@pytest.mark.postgres
def test_source_control_actor_is_session_login_under_set_role(
    control_engines: tuple[Engine, Engine, Engine],
) -> None:
    owner, _control, _scheduler = control_engines
    source_id, key, _ = _source(owner)
    with owner.begin() as connection:
        login = connection.execute(text("SELECT session_user")).scalar_one()
        connection.execute(text("SET LOCAL ROLE primary_signal_cap_source_control"))
        changed = set_source_enabled(
            connection, key, enabled=False, reason="Role-switched synthetic decision"
        )
    assert changed.changed
    assert _events(owner, source_id)[0]["actor"] == login


@pytest.mark.postgres
def test_source_control_capability_exposes_only_its_transition(
    control_engines: tuple[Engine, Engine, Engine],
) -> None:
    owner, control, _scheduler = control_engines
    source_id, key, _ = _source(owner)
    with control.begin() as connection:
        assert set_source_enabled(connection, key, enabled=False, reason="Privilege proof").changed
    for statement in (
        "SELECT * FROM primary_signal.sources LIMIT 1",
        "SELECT homepage_url FROM primary_signal.sources LIMIT 1",
        "INSERT INTO primary_signal.sources DEFAULT VALUES",
        "UPDATE primary_signal.sources SET enabled=enabled WHERE false",
        "DELETE FROM primary_signal.sources WHERE false",
        "SELECT * FROM primary_signal.source_state_events LIMIT 1",
        "INSERT INTO primary_signal.source_state_events DEFAULT VALUES",
        "UPDATE primary_signal.source_state_events SET reason=reason WHERE false",
        "DELETE FROM primary_signal.source_state_events WHERE false",
        "SELECT payload FROM primary_signal.jobs LIMIT 1",
        "SELECT error_detail FROM primary_signal.job_attempts LIMIT 1",
        "SELECT extracted_text FROM primary_signal.content_versions LIMIT 1",
        "SELECT primary_signal.lock_source_enabled('00000000-0000-0000-0000-000000000000'::uuid)",
    ):
        with pytest.raises(DBAPIError) as caught, control.begin() as connection:
            connection.execute(text(statement))
        assert (
            isinstance(caught.value.orig, psycopg.Error) and caught.value.orig.sqlstate == "42501"
        )
    with owner.connect() as connection:
        assert connection.execute(
            text(
                "SELECT NOT(rolcanlogin OR rolsuper OR rolinherit OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls) FROM pg_catalog.pg_roles WHERE rolname=:role"
            ),
            {"role": ROLE},
        ).scalar_one()
        assert connection.execute(
            text("SELECT has_function_privilege(:role,:function,'EXECUTE')"),
            {"role": ROLE, "function": FUNCTION},
        ).scalar_one()
        for role in (
            "public",
            "primary_signal_cap_public_read",
            "primary_signal_cap_publication_write",
            "primary_signal_cap_editorial_read",
            "primary_signal_cap_queue_consume",
            "primary_signal_cap_job_health",
        ):
            assert not connection.execute(
                text("SELECT has_function_privilege(:role,:function,'EXECUTE')"),
                {"role": role, "function": FUNCTION},
            ).scalar_one()
        config = connection.execute(
            text(
                "SELECT prosecdef,proconfig FROM pg_catalog.pg_proc WHERE oid=CAST(:function AS regprocedure)"
            ),
            {"function": FUNCTION},
        ).one()
        assert config.prosecdef
        assert "search_path=pg_catalog" in config.proconfig
        assert (
            connection.execute(
                select(func.count())
                .select_from(SourceStateEvent)
                .where(SourceStateEvent.source_id == source_id)
            ).scalar_one()
            == 1
        )


@pytest.mark.postgres
@pytest.mark.parametrize("second_enabled", [False, True])
def test_concurrent_source_decisions_serialize_and_clock_follows_lock(
    control_engines: tuple[Engine, Engine, Engine], second_enabled: bool
) -> None:
    owner, control, _scheduler = control_engines
    source_id, key, _ = _source(owner)
    started = threading.Event()
    second_pid: list[int] = []

    def decide() -> SourceControlResult:
        with control.begin() as connection:
            second_pid.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()
            return set_source_enabled(
                connection, key, enabled=second_enabled, reason="Second synthetic decision"
            )

    with control.connect() as first, ThreadPoolExecutor(max_workers=1) as executor:
        transaction = first.begin()
        try:
            first_pid = first.execute(text("SELECT pg_backend_pid()")).scalar_one()
            initial = set_source_enabled(
                first, key, enabled=False, reason="First synthetic decision"
            )
            future = executor.submit(decide)
            assert started.wait(2)
            _wait_for_block(owner, second_pid[0], first_pid)
            release_time = first.execute(text("SELECT clock_timestamp()")).scalar_one()
            transaction.commit()
            second = future.result(timeout=5)
        finally:
            if transaction.is_active:
                transaction.rollback()
    events = _events(owner, source_id)
    if second_enabled:
        assert second.changed and second.updated_at >= release_time
        assert [(row["old_enabled"], row["new_enabled"]) for row in events] == [
            (True, False),
            (False, True),
        ]
    else:
        assert not second.changed and second.event_id is None
        assert second.updated_at == initial.updated_at
        assert len(events) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("pause_first", [True, False])
def test_source_pause_and_scheduler_serialize_in_both_lock_orders(
    control_engines: tuple[Engine, Engine, Engine], pause_first: bool
) -> None:
    owner, control, scheduler = control_engines
    _source_id, key, feed_id = _source(owner, feed=True)
    assert feed_id is not None
    started = threading.Event()
    waiting_pid: list[int] = []

    def schedule() -> ScheduleSummary:
        with scheduler.begin() as connection:
            waiting_pid.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()
            return FeedScheduleRepository(connection, build_default_catalogue()).schedule_due(
                limit=1
            )

    def pause() -> SourceControlResult:
        with control.begin() as connection:
            waiting_pid.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()
            return set_source_enabled(
                connection, key, enabled=False, reason="Synthetic scheduled pause"
            )

    with owner.connect() as connection:
        original = dict(
            connection.execute(select(Feed.__table__).where(Feed.id == feed_id)).mappings().one()
        )
    holder = control if pause_first else scheduler
    with holder.connect() as first, ThreadPoolExecutor(max_workers=1) as executor:
        transaction = first.begin()
        try:
            first_pid = first.execute(text("SELECT pg_backend_pid()")).scalar_one()
            if pause_first:
                set_source_enabled(first, key, enabled=False, reason="Pause before scheduling")
                future = executor.submit(schedule)
            else:
                summary = FeedScheduleRepository(first, build_default_catalogue()).schedule_due(
                    limit=1
                )
                assert summary == ScheduleSummary(1, 1, 0)
                future = executor.submit(pause)
            assert started.wait(2)
            _wait_for_block(owner, waiting_pid[0], first_pid)
            transaction.commit()
            result = future.result(timeout=5)
        finally:
            if transaction.is_active:
                transaction.rollback()
    with owner.connect() as connection:
        current = dict(
            connection.execute(select(Feed.__table__).where(Feed.id == feed_id)).mappings().one()
        )
        jobs = (
            connection.execute(select(Job.id).where(Job.deduplication_key == f"feed:{feed_id}"))
            .scalars()
            .all()
        )
    if pause_first:
        assert result == ScheduleSummary(0, 0, 0)
        assert current == original
        assert jobs == []
    else:
        assert isinstance(result, SourceControlResult)
        assert result.changed
        assert len(jobs) == 1
        assert current["next_poll_at"] > original["next_poll_at"]
        assert {k: v for k, v in current.items() if k not in {"next_poll_at", "updated_at"}} == {
            k: v for k, v in original.items() if k not in {"next_poll_at", "updated_at"}
        }
