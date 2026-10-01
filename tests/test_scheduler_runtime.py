import logging
import signal
import threading
from collections.abc import Callable
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

from primary_signal.config import SchedulerSettings
from primary_signal.entrypoints.scheduler import (
    SchedulerRuntime,
    StopEvent,
    database_sqlstate,
    is_transient_database_error,
    retry_delay,
    stopping_on_signals,
)
from primary_signal.sources import ScheduleSummary


class FakeDatabaseError(Exception):
    sqlstate: str

    def __init__(self, sqlstate: str) -> None:
        super().__init__("detail that must not be logged")
        self.sqlstate = sqlstate


def database_error(sqlstate: str, *, connection_invalidated: bool = False) -> DBAPIError:
    return DBAPIError(
        statement=None,
        params=None,
        orig=FakeDatabaseError(sqlstate),
        connection_invalidated=connection_invalidated,
    )


class FakeEvent:
    def __init__(self, *, stop_after_waits: int | None = None) -> None:
        self.set_called = False
        self.waits: list[float | None] = []
        self._stop_after_waits = stop_after_waits

    def is_set(self) -> bool:
        return self.set_called

    def set(self) -> None:
        self.set_called = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if self._stop_after_waits is not None and len(self.waits) >= self._stop_after_waits:
            self.set()
        return self.set_called


class FakeScheduler:
    def __init__(self, outcomes: list[ScheduleSummary | BaseException]) -> None:
        self.outcomes = iter(outcomes)
        self.limits: list[int] = []

    def schedule_due(self, *, limit: int = 100) -> ScheduleSummary:
        self.limits.append(limit)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.parametrize(
    "sqlstate",
    [
        "08001",
        "08006",
        "40001",
        "40P01",
        "53000",
        "53100",
        "55P03",
        "57014",
        "57P01",
        "57P02",
        "57P03",
    ],
)
def test_transient_database_errors_are_classified_by_sqlstate(sqlstate: str) -> None:
    error = database_error(sqlstate)

    assert database_sqlstate(error) == sqlstate
    assert is_transient_database_error(error) is True


@pytest.mark.parametrize("sqlstate", ["28000", "28P01", "42501", "42P01", "22000"])
def test_configuration_privilege_schema_and_data_errors_are_fatal(sqlstate: str) -> None:
    assert is_transient_database_error(database_error(sqlstate)) is False


def test_non_database_exception_with_sqlstate_is_still_fatal() -> None:
    assert is_transient_database_error(FakeDatabaseError("40001")) is False


def test_initial_connection_error_without_sqlstate_is_transient() -> None:
    error = OperationalError(None, None, OSError("private connection detail"))

    assert database_sqlstate(error) is None
    assert error.connection_invalidated is False
    assert is_transient_database_error(error) is True


def test_equal_jitter_retry_is_exponential_and_capped() -> None:
    settings = SchedulerSettings(retry_initial_seconds=2, retry_max_seconds=5)

    assert retry_delay(settings, 1, lambda: 0) == 1
    assert retry_delay(settings, 2, lambda: 1) == 4
    assert retry_delay(settings, 3, lambda: 0.5) == 3.75

    with pytest.raises(ValueError, match="positive"):
        retry_delay(settings, 0, lambda: 0.5)


def test_once_runs_immediately_without_waiting() -> None:
    scheduler = FakeScheduler([ScheduleSummary(selected=0, enqueued=0, already_active=0)])
    stop_event = FakeEvent()

    SchedulerRuntime(
        scheduler,
        SchedulerSettings(batch_size=25),
        cast(StopEvent, stop_event),
    ).run(once=True)

    assert scheduler.limits == [25]
    assert stop_event.waits == []


def test_full_batches_run_again_immediately_then_idle_waits() -> None:
    scheduler = FakeScheduler(
        [
            ScheduleSummary(selected=2, enqueued=2, already_active=0),
            ScheduleSummary(selected=1, enqueued=0, already_active=1),
        ]
    )
    stop_event = FakeEvent(stop_after_waits=1)

    SchedulerRuntime(
        scheduler,
        SchedulerSettings(batch_size=2, poll_interval_seconds=7),
        cast(StopEvent, stop_event),
    ).run()

    assert scheduler.limits == [2, 2]
    assert stop_event.waits == [7]


def test_transient_failure_retries_with_interruptible_wait() -> None:
    scheduler = FakeScheduler(
        [
            database_error("40001"),
            ScheduleSummary(selected=0, enqueued=0, already_active=0),
        ]
    )
    stop_event = FakeEvent(stop_after_waits=2)

    SchedulerRuntime(
        scheduler,
        SchedulerSettings(retry_initial_seconds=2, poll_interval_seconds=9),
        cast(StopEvent, stop_event),
        random_value=lambda: 0,
    ).run()

    assert scheduler.limits == [100, 100]
    assert stop_event.waits == [1, 9]


def test_once_and_fatal_errors_never_retry() -> None:
    for once, error in (
        (True, database_error("40001")),
        (False, database_error("42501")),
        (False, RuntimeError("invariant")),
    ):
        stop_event = FakeEvent()
        runtime = SchedulerRuntime(
            FakeScheduler([error]),
            SchedulerSettings(),
            cast(StopEvent, stop_event),
        )

        with pytest.raises(type(error)):
            runtime.run(once=once)
        assert stop_event.waits == []


def test_work_logs_at_info_and_idle_at_debug() -> None:
    scheduler = FakeScheduler(
        [
            ScheduleSummary(selected=1, enqueued=1, already_active=0),
            ScheduleSummary(selected=0, enqueued=0, already_active=0),
        ]
    )
    stop_event = FakeEvent(stop_after_waits=2)

    with patch("primary_signal.entrypoints.scheduler.log_event") as log_event:
        SchedulerRuntime(
            scheduler,
            SchedulerSettings(batch_size=2),
            cast(StopEvent, stop_event),
        ).run()

    completed = [
        invocation
        for invocation in log_event.call_args_list
        if invocation.args[2] == "scheduler.pass.completed"
    ]
    assert [invocation.args[1] for invocation in completed] == [logging.INFO, logging.DEBUG]


def test_signal_handlers_only_set_event_and_are_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[signal.Signals, object] = {}
    restored_handlers = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }

    def fake_getsignal(signum: signal.Signals) -> object:
        return restored_handlers[signum]

    def fake_signal(signum: signal.Signals, handler: object) -> object:
        installed[signum] = handler
        return restored_handlers[signum]

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)
    stop_event = FakeEvent()

    with stopping_on_signals(cast(threading.Event, stop_event)):
        handler = cast(Callable[[int, object], None], installed[signal.SIGTERM])
        handler(signal.SIGTERM, None)
        assert stop_event.set_called is True

    assert installed == restored_handlers
