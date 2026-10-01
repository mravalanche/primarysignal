"""Schedule due feed work in bounded database transactions."""

import argparse
import logging
import random
import signal
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Protocol, cast

from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, OperationalError

from primary_signal.config import SchedulerSettings, Settings
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.jobs import build_default_catalogue
from primary_signal.observability import configure_logging, log_event, log_exception
from primary_signal.sources import ScheduleSummary, TransactionalFeedScheduler

LOGGER = logging.getLogger(__name__)
TRANSIENT_SQLSTATE_PREFIXES = ("08", "53")
TRANSIENT_SQLSTATES = frozenset({"40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03"})


class Scheduler(Protocol):
    """The small scheduling surface needed by the process loop."""

    def schedule_due(self, *, limit: int = 100) -> ScheduleSummary: ...


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


type RandomValue = Callable[[], float]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one scheduling pass and exit")
    return parser


def database_sqlstate(exception: BaseException) -> str | None:
    """Return a driver SQLSTATE without exposing its error message."""

    candidate: BaseException | None = exception
    if isinstance(exception, DBAPIError) and isinstance(exception.orig, BaseException):
        candidate = exception.orig
    value = getattr(candidate, "sqlstate", None)
    return value if isinstance(value, str) else None


def is_transient_database_error(exception: BaseException) -> bool:
    """Classify only narrowly defined, operational PostgreSQL failures for retry."""

    if not isinstance(exception, DBAPIError):
        return False
    if exception.connection_invalidated:
        return True
    sqlstate = database_sqlstate(exception)
    if isinstance(exception, OperationalError) and sqlstate is None:
        return True
    return bool(
        sqlstate
        and (sqlstate.startswith(TRANSIENT_SQLSTATE_PREFIXES) or sqlstate in TRANSIENT_SQLSTATES)
    )


def retry_delay(settings: SchedulerSettings, attempt: int, random_value: RandomValue) -> float:
    """Calculate capped exponential equal jitter for a one-based retry attempt."""

    if attempt < 1:
        raise ValueError("retry attempt must be positive")
    ceiling = min(
        settings.retry_initial_seconds * (2 ** min(attempt - 1, 16)),
        settings.retry_max_seconds,
    )
    return (ceiling / 2) + (ceiling / 2 * min(max(random_value(), 0.0), 1.0))


class SchedulerRuntime:
    """Run bounded scheduler passes until stopped."""

    def __init__(
        self,
        scheduler: Scheduler,
        settings: SchedulerSettings,
        stop_event: StopEvent,
        *,
        random_value: RandomValue = random.random,
    ) -> None:
        self._scheduler = scheduler
        self._settings = settings
        self._stop_event = stop_event
        self._random_value = random_value

    def run(self, *, once: bool = False) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                summary = self._scheduler.schedule_due(limit=self._settings.batch_size)
            except Exception as exception:
                transient = is_transient_database_error(exception)
                if once or not transient:
                    log_exception(
                        LOGGER,
                        "scheduler.pass.failed",
                        exception,
                        result="fatal" if not transient else "once",
                    )
                    raise
                failures += 1
                delay = retry_delay(self._settings, failures, self._random_value)
                log_exception(
                    LOGGER,
                    "scheduler.pass.failed",
                    exception,
                    result="retry",
                    retry_attempt=failures,
                    retry_seconds=delay,
                )
                self._stop_event.wait(delay)
                continue

            failures = 0
            level = logging.INFO if summary.selected else logging.DEBUG
            log_event(
                LOGGER,
                level,
                "scheduler.pass.completed",
                selected=summary.selected,
                enqueued=summary.enqueued,
                already_active=summary.already_active,
                batch_size=self._settings.batch_size,
                result="work" if summary.selected else "idle",
            )
            if once:
                return
            if summary.selected < self._settings.batch_size:
                self._stop_event.wait(self._settings.poll_interval_seconds)


@contextmanager
def stopping_on_signals(stop_event: threading.Event) -> Generator[None]:
    """Set an event on termination signals and restore the previous handlers."""

    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in watched}

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    try:
        for signum in watched:
            signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def scheduler_database_settings() -> DatabaseSettings:
    """Load database credentials while fixing scheduler-specific pool behaviour."""

    load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
    return load_settings().model_copy(
        update={
            "application_name": "primary_signal_scheduler",
            "pool_size": 1,
            "max_overflow": 0,
        }
    )


def run_with_engine(
    engine: Engine,
    settings: SchedulerSettings,
    stop_event: threading.Event,
    *,
    once: bool,
) -> None:
    scheduler = TransactionalFeedScheduler(engine, build_default_catalogue())
    SchedulerRuntime(scheduler, settings, stop_event).run(once=once)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the scheduler with environment-only configuration."""

    args = _parser().parse_args(argv)
    # Establish the safe formatter before loading configuration or opening a
    # connection so startup failures cannot fall back to a raw traceback.
    configure_logging(level="INFO")
    engine: Engine | None = None
    stop_event = threading.Event()
    started = False
    result = "failed"
    exit_code = 0
    try:
        application_settings = Settings()
        scheduler_settings = SchedulerSettings()
        configure_logging(level=application_settings.log_level.value)
        engine = create_database_engine(scheduler_database_settings())
        started = True
        log_event(
            LOGGER,
            logging.INFO,
            "scheduler.started",
            batch_size=scheduler_settings.batch_size,
            result="once" if cast(bool, args.once) else "continuous",
        )
        with stopping_on_signals(stop_event):
            run_with_engine(engine, scheduler_settings, stop_event, once=cast(bool, args.once))
        if stop_event.is_set():
            log_event(LOGGER, logging.INFO, "scheduler.stopping", result="signal")
        result = "completed" if cast(bool, args.once) else "stopped"
    except Exception as exception:
        log_exception(LOGGER, "scheduler.failed", exception, result="fatal")
        exit_code = 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exception:
                log_exception(LOGGER, "scheduler.dispose.failed", exception, result="fatal")
                result = "failed"
                exit_code = 1
        if started:
            log_event(LOGGER, logging.INFO, "scheduler.stopped", result=result)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
