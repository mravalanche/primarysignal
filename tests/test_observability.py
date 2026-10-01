import json
import logging
import sys

from primary_signal.observability.logging import (
    JsonFormatter,
    StripAccessQueryFilter,
    configure_logging,
    log_event,
    log_exception,
    sanitize_url,
    sanitize_value,
)


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, object]] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.documents.append(json.loads(self.format(record)))


def test_recursive_redaction_and_url_sanitization() -> None:
    value = {
        "nested": {
            "access_token": "do-not-log",
            "url": "https://reader:private@public.example/article?id=private#private",  # pragma: allowlist secret
        },
        "items": [{"password": "do-not-log"}],  # pragma: allowlist secret
    }

    assert sanitize_value("/stories?token=private#draft", key="path") == "/stories"

    assert sanitize_value(value) == {
        "nested": {
            "access_token": "[redacted]",
            "url": "https://public.example/article",
        },
        "items": [{"password": "[redacted]"}],
    }


def test_url_sanitizer_handles_relative_and_invalid_urls() -> None:
    assert sanitize_url("/stories?preview=private#draft") == "/stories"
    assert sanitize_url("https://[invalid") == "[invalid-url]"
    assert sanitize_url("https:///missing-host") == "[invalid-url]"
    assert sanitize_url("https://public.example:invalid/story") == "[invalid-url]"


def test_json_logging_uses_allowlisted_fields() -> None:
    logger = logging.getLogger("tests.safe-log")
    logger.propagate = False
    handler = CapturingHandler()
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "request.finished",
        method="GET",
        url="https://public.example/story?token=private",
        arbitrary_private_value="not allowed",
    )

    assert handler.documents[0]["event"] == "request.finished"
    assert handler.documents[0]["url"] == "https://public.example/story"
    assert "arbitrary_private_value" not in handler.documents[0]


def test_scheduler_numeric_fields_are_allowlisted_and_bounded() -> None:
    logger = logging.getLogger("tests.scheduler-log")
    logger.propagate = False
    handler = CapturingHandler()
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "scheduler.pass.completed",
        selected=4,
        enqueued=3,
        already_active=1,
        batch_size=100,
        retry_seconds=float("inf"),
        retry_attempt=-1,
    )

    document = handler.documents[0]
    assert document["selected"] == 4
    assert document["enqueued"] == 3
    assert document["already_active"] == 1
    assert document["batch_size"] == 100
    assert "retry_seconds" not in document
    assert "retry_attempt" not in document


def test_formatter_never_emits_arbitrary_log_messages() -> None:
    record = logging.LogRecord(
        "third.party",
        logging.ERROR,
        __file__,
        1,
        "Authorization: Bearer very-secret-token api_key=also-secret host=private-db user=operator",
        (),
        None,
    )

    document = json.loads(JsonFormatter().format(record))

    assert document["event"] == "external.log"
    assert "secret" not in json.dumps(document)
    assert "private-db" not in json.dumps(document)


def test_event_names_are_machine_readable_not_free_text() -> None:
    logger = logging.getLogger("tests.event-name")
    logger.propagate = False
    handler = CapturingHandler()
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)

    log_event(logger, logging.INFO, "Authorization: Bearer private")

    assert handler.documents[0]["event"] == "application.log"
    assert "private" not in json.dumps(handler.documents[0])


def test_exception_logging_omits_message_and_traceback() -> None:
    logger = logging.getLogger("tests.safe-exception")
    logger.propagate = False
    handler = CapturingHandler()
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)
    exception = RuntimeError("credential must not appear")

    log_exception(logger, "processing.failed", exception)

    document = handler.documents[0]
    assert document["error_type"] == "RuntimeError"
    assert "credential" not in json.dumps(document)


def test_uvicorn_access_filter_strips_query_from_target() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.10", "GET", "/stories?token=private", "1.1", 200),
        None,
    )

    assert StripAccessQueryFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[2] == "/stories"

    record.args = {"path": "/stories?private=true"}
    assert StripAccessQueryFilter().filter(record) is True


def test_sanitizer_bounds_strings_and_converts_safe_scalar_types() -> None:
    assert sanitize_value("x" * 2_000) == ("x" * 1_024) + "…"
    assert sanitize_value((True, 3, None)) == [True, 3, None]
    assert sanitize_value("password=private, next") == "password=[redacted], next"
    assert sanitize_value({"api_key": "private"}) == {  # pragma: allowlist secret
        "api_key": "[redacted]"
    }
    assert (
        sanitize_value(  # pragma: allowlist secret
            "postgresql://reader:private@db.public.example/app?sslmode=require"  # pragma: allowlist secret
        )
        == "postgresql://db.public.example/app"
    )
    converted = sanitize_value(object())
    assert isinstance(converted, str)
    assert converted.startswith("<object object at")


def test_formatter_records_exception_type_without_traceback() -> None:
    try:
        raise LookupError("private detail")
    except LookupError:
        exception_info = sys.exc_info()

    record = logging.LogRecord(
        "tests.exception",
        logging.ERROR,
        __file__,
        1,
        "failed",
        (),
        exception_info,
    )

    document = json.loads(JsonFormatter().format(record))
    assert document["error_type"] == "LookupError"
    assert "private detail" not in json.dumps(document)


def test_configure_logging_sets_json_handler_and_access_filter() -> None:
    configure_logging(level="WARNING")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert any(
        isinstance(item, StripAccessQueryFilter)
        for item in logging.getLogger("uvicorn.access").filters
    )
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
