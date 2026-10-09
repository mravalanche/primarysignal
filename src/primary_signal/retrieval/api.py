"""Small internal HTTP boundary for bounded feed retrieval."""

import base64
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.extract import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    NORMALIZATION_VERSION,
)
from primary_signal.retrieval.http import fetch_article as retrieve_article
from primary_signal.retrieval.http import fetch_feed
from primary_signal.retrieval.policy import (
    PolicyError,
    _canonical_host,  # pyright: ignore[reportPrivateUsage]
)

_TRANSPORT_FEED = fetch_feed
_TRANSPORT_ARTICLE = retrieve_article

MAX_REQUEST_BYTES = 16 * 1024
DEFAULT_MAX_CONCURRENT_FETCHES = 8
DEFAULT_MAX_CONCURRENT_PER_ORIGIN = 2
DEFAULT_MAX_REQUESTS_GLOBAL = 240
DEFAULT_GLOBAL_WINDOW_SECONDS = 60.0
DEFAULT_MAX_REQUESTS_PER_ORIGIN = 30
DEFAULT_ORIGIN_WINDOW_SECONDS = 60.0
DEFAULT_MAX_TRACKED_ORIGINS = 512

_SAFE_FETCH_ERRORS = frozenset(
    {
        "ambiguous_ip",
        "authority",
        "dns_answers",
        "dns_error",
        "fetch_network",
        "fetch_timeout",
        "forbidden_address",
        "host",
        "http_status",
        "https_downgrade",
        "invalid_redirect",
        "invalid_response",
        "invalid_response_body",
        "invalid_response_headers",
        "noncanonical_ip",
        "peer_mismatch",
        "port",
        "redirect_loop",
        "response_too_large",
        "scheme",
        "unsupported_content_encoding",
        "unsupported_transfer_encoding",
        "url",
        "unsupported_content_type",
        "unsupported_charset",
        "invalid_body_size",
        "invalid_encoding",
        "html_too_complex",
        "invalid_html",
        "empty_content",
        "output_too_large",
    }
)

type FeedFetch = Callable[[str, str | None, str | None], FeedFetchResult]
type ArticleFetch = Callable[[str, str | None, str | None], ArticleFetchResult]


def _default_fetch(url: str, etag: str | None, last_modified: str | None) -> FeedFetchResult:
    return fetch_feed(url, etag=etag, last_modified=last_modified)


def _default_fetch_article(
    url: str, etag: str | None, last_modified: str | None
) -> ArticleFetchResult:
    return retrieve_article(url, etag=etag, last_modified=last_modified)


class _FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str
    etag: str | None = None
    last_modified: str | None = None


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _origin_hostname(url: str) -> str:
    """Get the policy's canonical host without performing a DNS lookup."""

    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise PolicyError("scheme")
        hostname, raw_port, _ = _canonical_host(parsed.netloc)
        expected_port = 80 if parsed.scheme.lower() == "http" else 443
        if raw_port is not None and (
            not raw_port
            or not raw_port.isascii()
            or not raw_port.isdecimal()
            or int(raw_port, 10) != expected_port
        ):
            raise PolicyError("port")
    except PolicyError:
        raise
    except ValueError:
        raise PolicyError("url") from None
    return hostname


@dataclass(slots=True)
class _RateBucket:
    tokens: float
    updated: float

    def refill(self, *, now: float, capacity: int, window_seconds: float) -> None:
        self.tokens = min(
            float(capacity),
            self.tokens + max(0.0, now - self.updated) * capacity / window_seconds,
        )
        self.updated = now


@dataclass(slots=True)
class _OriginState:
    bucket: _RateBucket
    active: int = 0


class _RedirectAdmissionError(FeedFetchError):
    """Admission failure from the API's validated redirect hook."""


class _RequestLimiter:
    """Atomically admit requests and validated redirect hops under host limits."""

    def __init__(
        self,
        *,
        max_requests_global: int,
        global_window_seconds: float,
        max_concurrent_per_origin: int,
        max_requests_per_origin: int,
        origin_window_seconds: float,
        max_origins: int,
    ) -> None:
        self.max_requests_global = max_requests_global
        self.global_window_seconds = global_window_seconds
        self.max_concurrent_per_origin = max_concurrent_per_origin
        self.max_requests_per_origin = max_requests_per_origin
        self.origin_window_seconds = origin_window_seconds
        self.max_origins = max_origins
        self._global_bucket = _RateBucket(float(max_requests_global), monotonic())
        self._states: OrderedDict[str, _OriginState] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, hostname: str) -> str | None:
        return self._acquire(hostname, charge_global=True, hold_origin=True)

    def _acquire(self, hostname: str, *, charge_global: bool, hold_origin: bool) -> str | None:
        with self._lock:
            now = monotonic()
            if charge_global:
                self._global_bucket.refill(
                    now=now,
                    capacity=self.max_requests_global,
                    window_seconds=self.global_window_seconds,
                )
                if self._global_bucket.tokens < 1:
                    return "global_rate_limited"
            state = self._states.get(hostname)
            if state is None:
                if len(self._states) >= self.max_origins:
                    # Retain active and recent buckets so host rotation cannot reset rates.
                    for old_host, old_state in tuple(self._states.items()):
                        if (
                            old_state.active == 0
                            and now - old_state.bucket.updated >= self.origin_window_seconds
                        ):
                            del self._states[old_host]
                            break
                    else:
                        return "retriever_busy"
                state = _OriginState(_RateBucket(float(self.max_requests_per_origin), now))
                self._states[hostname] = state
            else:
                self._states.move_to_end(hostname)
            state.bucket.refill(
                now=now,
                capacity=self.max_requests_per_origin,
                window_seconds=self.origin_window_seconds,
            )
            if hold_origin and state.active >= self.max_concurrent_per_origin:
                return "retriever_busy"
            if state.bucket.tokens < 1:
                return "origin_rate_limited"
            if charge_global:
                self._global_bucket.tokens -= 1
            state.bucket.tokens -= 1
            if hold_origin:
                state.active += 1
        return None

    def release(self, hostname: str) -> None:
        with self._lock:
            self._states[hostname].active -= 1

    @contextmanager
    def redirect_lease(self, hostname: str, *, initial_hostname: str) -> Generator[None]:
        # The initial host's active lease spans the full fetch. A same-host
        # redirect spends another token while sharing that lease.
        hold_origin = hostname != initial_hostname
        rejected = self._acquire(hostname, charge_global=False, hold_origin=hold_origin)
        if rejected is not None:
            raise _RedirectAdmissionError(rejected)
        try:
            yield
        finally:
            if hold_origin:
                self.release(hostname)


def create_retriever_app(
    *,
    fetch: FeedFetch = _default_fetch,
    fetch_article: ArticleFetch = _default_fetch_article,
    max_concurrent_fetches: int = DEFAULT_MAX_CONCURRENT_FETCHES,
    max_requests_global: int = DEFAULT_MAX_REQUESTS_GLOBAL,
    global_window_seconds: float = DEFAULT_GLOBAL_WINDOW_SECONDS,
    max_concurrent_per_origin: int = DEFAULT_MAX_CONCURRENT_PER_ORIGIN,
    max_requests_per_origin: int = DEFAULT_MAX_REQUESTS_PER_ORIGIN,
    origin_window_seconds: float = DEFAULT_ORIGIN_WINDOW_SECONDS,
    max_tracked_origins: int = DEFAULT_MAX_TRACKED_ORIGINS,
) -> FastAPI:
    """Create an internal retriever with no database connection or durable state."""

    if max_concurrent_fetches < 1:
        raise ValueError("max_concurrent_fetches must be positive")
    if (
        max_requests_global < 1
        or not math.isfinite(global_window_seconds)
        or global_window_seconds <= 0
        or max_concurrent_per_origin < 1
        or max_requests_per_origin < 1
        or not math.isfinite(origin_window_seconds)
        or origin_window_seconds <= 0
        or max_tracked_origins < 1
    ):
        raise ValueError("retriever rate limits must be positive and finite")
    slots = threading.BoundedSemaphore(max_concurrent_fetches)
    limits = _RequestLimiter(
        max_requests_global=max_requests_global,
        global_window_seconds=global_window_seconds,
        max_concurrent_per_origin=max_concurrent_per_origin,
        max_requests_per_origin=max_requests_per_origin,
        origin_window_seconds=origin_window_seconds,
        max_origins=max_tracked_origins,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/feeds/fetch")
    async def retrieve(request: Request) -> JSONResponse:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return _error(400, "invalid_request")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    return _error(413, "request_too_large")
            except ValueError:
                return _error(400, "invalid_request")

        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(chunk) > MAX_REQUEST_BYTES - len(body):
                    return _error(413, "request_too_large")
                body.extend(chunk)
            payload = json.loads(body)
            parsed = _FetchRequest.model_validate(payload)
        except json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError:
            return _error(400, "invalid_request")
        try:
            hostname = _origin_hostname(parsed.url)
        except PolicyError as error:
            return _error(502, error.code)
        if not slots.acquire(blocking=False):
            return _error(503, "retriever_busy")
        rejected = limits.acquire(hostname)
        if rejected is not None:
            slots.release()
            return _error(
                429 if rejected in {"global_rate_limited", "origin_rate_limited"} else 503,
                rejected,
            )
        try:
            try:
                if fetch is _default_fetch and fetch_feed is _TRANSPORT_FEED:
                    result = await run_in_threadpool(
                        lambda: fetch_feed(
                            parsed.url,
                            etag=parsed.etag,
                            last_modified=parsed.last_modified,
                            hop_lease=lambda destination: limits.redirect_lease(
                                destination, initial_hostname=hostname
                            ),
                        )
                    )
                else:
                    result = await run_in_threadpool(
                        lambda: fetch(parsed.url, parsed.etag, parsed.last_modified)
                    )
            except _RedirectAdmissionError as error:
                return _error(429 if error.code == "origin_rate_limited" else 503, error.code)
            except FeedFetchError as error:
                return _error(
                    502, error.code if error.code in _SAFE_FETCH_ERRORS else "fetch_failed"
                )
            except Exception:
                return _error(502, "fetch_failed")
        finally:
            limits.release(hostname)
            slots.release()
        return JSONResponse(
            {
                "status": result.status,
                "final_url": result.final_url,
                "body_base64": base64.b64encode(result.body).decode("ascii"),
                "etag": result.etag,
                "last_modified": result.last_modified,
            }
        )

    @app.post("/v1/articles/fetch")
    async def retrieve_article_endpoint(request: Request) -> JSONResponse:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return _error(400, "invalid_request")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    return _error(413, "request_too_large")
            except ValueError:
                return _error(400, "invalid_request")
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(chunk) > MAX_REQUEST_BYTES - len(body):
                    return _error(413, "request_too_large")
                body.extend(chunk)
            parsed = _FetchRequest.model_validate(json.loads(body))
        except json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError:
            return _error(400, "invalid_request")
        try:
            hostname = _origin_hostname(parsed.url)
        except PolicyError as error:
            return _error(502, error.code)
        if not slots.acquire(blocking=False):
            return _error(503, "retriever_busy")
        rejected = limits.acquire(hostname)
        if rejected is not None:
            slots.release()
            return _error(
                429 if rejected in {"global_rate_limited", "origin_rate_limited"} else 503,
                rejected,
            )
        try:
            try:
                if (
                    fetch_article is _default_fetch_article
                    and retrieve_article is _TRANSPORT_ARTICLE
                ):
                    result = await run_in_threadpool(
                        lambda: retrieve_article(
                            parsed.url,
                            etag=parsed.etag,
                            last_modified=parsed.last_modified,
                            hop_lease=lambda destination: limits.redirect_lease(
                                destination, initial_hostname=hostname
                            ),
                        )
                    )
                else:
                    result = await run_in_threadpool(
                        lambda: fetch_article(parsed.url, parsed.etag, parsed.last_modified)
                    )
            except _RedirectAdmissionError as error:
                return _error(429 if error.code == "origin_rate_limited" else 503, error.code)
            except FeedFetchError as error:
                return _error(
                    502, error.code if error.code in _SAFE_FETCH_ERRORS else "fetch_failed"
                )
            except Exception:
                return _error(502, "fetch_failed")
        finally:
            limits.release(hostname)
            slots.release()
        extraction = result.extraction
        return JSONResponse(
            {
                "status": result.status,
                "final_url": result.final_url,
                "redirect_chain": [
                    {"status": hop.status, "url": hop.url} for hop in result.redirect_chain
                ],
                "content_type": result.content_type,
                "decoded_byte_count": result.decoded_byte_count,
                "raw_response_hash": result.raw_response_hash,
                "extracted_title": extraction.title if extraction else None,
                "extracted_text": extraction.text if extraction else None,
                "word_count": extraction.word_count if extraction else None,
                "normalized_content_hash": (
                    extraction.normalized_content_hash if extraction else None
                ),
                "extractor_name": EXTRACTOR_NAME if extraction else None,
                "extractor_version": EXTRACTOR_VERSION if extraction else None,
                "normalization_version": NORMALIZATION_VERSION if extraction else None,
                "etag": result.etag,
                "last_modified": result.last_modified,
            }
        )

    return app
