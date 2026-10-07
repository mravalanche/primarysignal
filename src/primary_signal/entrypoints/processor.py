"""Run durable ingestion jobs with fenced leases and explicit handlers."""

import argparse
import logging
import random
import threading
import uuid
from collections.abc import Callable, Sequence
from typing import cast

from sqlalchemy import Engine

from primary_signal.config import ProcessorSettings, Settings
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.entrypoints.scheduler import is_transient_database_error, stopping_on_signals
from primary_signal.jobs import (
    JobFailure,
    JobHandlers,
    JobLease,
    JobProcessingError,
    LostLease,
    TransactionalJobQueue,
    build_default_catalogue,
)
from primary_signal.observability import configure_logging, log_event, log_exception

LOGGER = logging.getLogger(__name__)


def retry_delay(
    settings: ProcessorSettings, attempt: int, random_value: Callable[[], float]
) -> float:
    """Return capped exponential equal jitter for a one-based database retry."""

    if attempt < 1:
        raise ValueError("retry attempt must be positive")
    ceiling = min(
        settings.retry_initial_seconds * (2 ** min(attempt - 1, 16)),
        settings.retry_max_seconds,
    )
    sample = min(max(random_value(), 0.0), 1.0)
    return ceiling / 2 + ceiling / 2 * sample


class ProcessorRuntime:
    """Process one job at a time; finish only while its lease is still owned."""

    def __init__(
        self,
        queue: TransactionalJobQueue,
        handlers: JobHandlers,
        settings: ProcessorSettings,
        stop_event: threading.Event,
        *,
        random_value: Callable[[], float] = random.random,
        worker_id: str | None = None,
    ) -> None:
        handlers.require_complete_queue("ingestion")
        self._queue = queue
        self._handlers = handlers
        self._settings = settings
        self._stop_event = stop_event
        self._random_value = random_value
        self._worker_id = worker_id or f"processor:{uuid.uuid4()}"

    def run(self, *, once: bool = False) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                self._queue.recover_expired(limit=self._settings.recovery_batch_size)
                lease = self._queue.claim(
                    queue="ingestion",
                    worker_id=self._worker_id,
                    lease_seconds=self._settings.lease_seconds,
                )
                if lease is not None:
                    self._process(lease)
            except Exception as exception:
                transient = is_transient_database_error(exception)
                if once or not transient:
                    log_exception(LOGGER, "processor.pass.failed", exception, result="fatal")
                    raise
                failures += 1
                delay = retry_delay(self._settings, failures, self._random_value)
                log_exception(
                    LOGGER,
                    "processor.pass.failed",
                    exception,
                    result="retry",
                    retry_attempt=failures,
                    retry_seconds=delay,
                )
                self._stop_event.wait(delay)
                continue
            failures = 0
            if once:
                return
            if lease is None:
                self._stop_event.wait(self._settings.poll_interval_seconds)

    def _process(self, lease: JobLease) -> None:
        handler = self._handlers.get(lease.job_type, lease.payload_version)
        heartbeat_stop = threading.Event()
        heartbeat_errors: list[Exception] = []

        def renew() -> None:
            while not heartbeat_stop.wait(self._settings.heartbeat_interval_seconds):
                try:
                    self._queue.heartbeat(lease, lease_seconds=self._settings.lease_seconds)
                except Exception as exception:
                    heartbeat_errors.append(exception)
                    return

        heartbeat = threading.Thread(target=renew, name="processor-heartbeat", daemon=True)
        heartbeat.start()
        callback = None
        handler_error: Exception | None = None
        try:
            callback = handler(lease)
        except Exception as exception:
            handler_error = exception
        finally:
            heartbeat_stop.set()
            heartbeat.join()

        if heartbeat_errors:
            if isinstance(heartbeat_errors[0], LostLease):
                log_event(
                    LOGGER, logging.WARNING, "processor.job.lease_lost", job_id=str(lease.job_id)
                )
                return
            raise heartbeat_errors[0]

        try:
            if handler_error is None:
                self._queue.succeed(lease, on_success=callback)
                log_event(LOGGER, logging.INFO, "processor.job.succeeded", job_id=str(lease.job_id))
            else:
                failure = (
                    handler_error.failure
                    if isinstance(handler_error, JobProcessingError)
                    else JobFailure(code="handler_error")
                )
                on_failure = (
                    handler_error.on_failure
                    if isinstance(handler_error, JobProcessingError)
                    else None
                )
                disposition = self._queue.fail(lease, failure, on_failure=on_failure)
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "processor.job.failed",
                    job_id=str(lease.job_id),
                    error_code=failure.code,
                    result=disposition.status,
                )
        except LostLease:
            log_event(LOGGER, logging.WARNING, "processor.job.lease_lost", job_id=str(lease.job_id))


def processor_database_settings() -> DatabaseSettings:
    """Load processor credentials with a pool for work and lease renewal."""

    load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
    return load_settings().model_copy(
        update={"application_name": "primary_signal_processor", "pool_size": 2, "max_overflow": 0}
    )


def run_with_engine(
    engine: Engine,
    settings: ProcessorSettings,
    stop_event: threading.Event,
    handlers: JobHandlers,
    *,
    once: bool,
) -> None:
    catalogue = build_default_catalogue()
    queue = TransactionalJobQueue(engine, catalogue)
    ProcessorRuntime(queue, handlers, settings, stop_event).run(once=once)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run only explicitly bound ingestion contracts."""

    args = _parser().parse_args(argv)
    configure_logging(level="INFO")
    engine: Engine | None = None
    exit_code = 0
    try:
        application_settings = Settings()
        settings = ProcessorSettings()
        configure_logging(level=application_settings.log_level.value)
        handlers = JobHandlers(build_default_catalogue(), ())
        handlers.require_complete_queue("ingestion")
        engine = create_database_engine(processor_database_settings())
        stop_event = threading.Event()
        log_event(
            LOGGER, logging.INFO, "processor.started", result="once" if args.once else "continuous"
        )
        with stopping_on_signals(stop_event):
            run_with_engine(engine, settings, stop_event, handlers, once=cast(bool, args.once))
        log_event(LOGGER, logging.INFO, "processor.stopped", result="completed")
    except Exception as exception:
        log_exception(LOGGER, "processor.failed", exception, result="fatal")
        exit_code = 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exception:
                log_exception(LOGGER, "processor.dispose.failed", exception, result="fatal")
                exit_code = 1
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
