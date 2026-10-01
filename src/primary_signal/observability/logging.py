"""Small stdlib JSON logger with an intentionally narrow data contract."""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

MAX_STRING_LENGTH: Final = 1_024
EVENT_FIELDS: Final = frozenset(
    {
        "duration_ms",
        "error_code",
        "error_type",
        "job_id",
        "kind",
        "method",
        "path",
        "queue",
        "request_id",
        "result",
        "service",
        "status_code",
        "surface",
        "url",
    }
)
SENSITIVE_KEY_PARTS: Final = (
    "authorization",
    "api_key",
    "cookie",
    "credential",
    "database_url",
    "dsn",
    "password",
    "secret",
    "token",
)
URL_PATTERN: Final = re.compile(r"[a-z][a-z0-9+.-]*://[^\s]+", re.IGNORECASE)
SECRET_ASSIGNMENT_PATTERN: Final = re.compile(
    r"\b(api_key|authorization|cookie|credential|database_url|dsn|password|secret|token)"
    r"\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
EVENT_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _safe_event_name(value: object) -> str:
    """Accept compact machine event names, never arbitrary message text."""

    if isinstance(value, str) and EVENT_NAME_PATTERN.fullmatch(value):
        return value
    return "application.log"


def sanitize_url(value: str) -> str:
    """Remove user information, query strings, and fragments from a URL."""

    try:
        parsed = urlsplit(value)
        if not parsed.scheme and not parsed.netloc:
            return parsed.path
        hostname = parsed.hostname
        if hostname is None:
            return "[invalid-url]"
        safe_host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            safe_host = f"{safe_host}:{parsed.port}"
        return urlunsplit(SplitResult(parsed.scheme, safe_host, parsed.path, "", ""))
    except ValueError:
        return "[invalid-url]"


def _sanitize_string(value: str) -> str:
    without_url_details = URL_PATTERN.sub(lambda match: sanitize_url(match.group()), value)
    without_assignments = SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}=[redacted]", without_url_details
    )
    if len(without_assignments) > MAX_STRING_LENGTH:
        return f"{without_assignments[:MAX_STRING_LENGTH]}…"
    return without_assignments


def sanitize_value(value: object, *, key: object | None = None) -> object:
    """Recursively redact sensitive mappings and bound values for safe JSON output."""

    if key is not None and _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, str) and str(key).casefold() in {"path", "url"}:
        return sanitize_url(value)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(child_key): sanitize_value(child_value, key=child_key)
            for child_key, child_value in mapping.items()
        }
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [sanitize_value(item) for item in sequence[:100]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _sanitize_string(str(value))


class JsonFormatter(logging.Formatter):
    """Serialize standard fields plus explicitly allowlisted event fields."""

    def format(self, record: logging.LogRecord) -> str:
        event = _safe_event_name(getattr(record, "primary_signal_event", "external.log"))
        document: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # Arbitrary LogRecord messages can contain driver errors, request
            # bodies, credentials, or local configuration. Only project events
            # created through log_event() are allowed into the public contract.
            "event": event,
        }
        supplied = getattr(record, "event_fields", {})
        if isinstance(supplied, Mapping):
            event_fields = cast(Mapping[object, object], supplied)
            for key in EVENT_FIELDS:
                if key in event_fields:
                    document[key] = sanitize_value(event_fields[key], key=key)
        if record.exc_info:
            exception = record.exc_info[1]
            document["error_type"] = type(exception).__name__ if exception else "Exception"
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False)


class StripAccessQueryFilter(logging.Filter):
    """Remove URL query strings and fragments from Uvicorn access records."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            mutable = list(args)
            mutable[2] = sanitize_url(args[2])
            record.args = tuple(mutable)
        return True


def configure_logging(*, level: str) -> None:
    """Configure application and Uvicorn logs for JSON output."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters.clear()
    access_logger.addFilter(StripAccessQueryFilter())
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: object,
) -> None:
    """Log one named event, ignoring fields outside the public log contract."""

    allowed = {key: value for key, value in fields.items() if key in EVENT_FIELDS}
    logger.log(
        level,
        event,
        extra={"event_fields": allowed, "primary_signal_event": _safe_event_name(event)},
    )


def log_exception(
    logger: logging.Logger,
    event: str,
    exception: BaseException,
    **fields: object,
) -> None:
    """Log an exception type without its potentially sensitive message or traceback."""

    fields["error_type"] = type(exception).__name__
    log_event(logger, logging.ERROR, event, **fields)
