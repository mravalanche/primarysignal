"""Small internal HTTP boundary for bounded feed retrieval."""

import base64
import json
import threading
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.retrieval.http import fetch_feed

MAX_REQUEST_BYTES = 16 * 1024
DEFAULT_MAX_CONCURRENT_FETCHES = 8

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
    }
)

type FeedFetch = Callable[[str, str | None, str | None], FeedFetchResult]


def _default_fetch(url: str, etag: str | None, last_modified: str | None) -> FeedFetchResult:
    return fetch_feed(url, etag=etag, last_modified=last_modified)


class _FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str
    etag: str | None = None
    last_modified: str | None = None


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def create_retriever_app(
    *,
    fetch: FeedFetch = _default_fetch,
    max_concurrent_fetches: int = DEFAULT_MAX_CONCURRENT_FETCHES,
) -> FastAPI:
    """Create an internal retriever with no database connection or state."""

    if max_concurrent_fetches < 1:
        raise ValueError("max_concurrent_fetches must be positive")
    slots = threading.BoundedSemaphore(max_concurrent_fetches)
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
        if not slots.acquire(blocking=False):
            return _error(503, "retriever_busy")
        try:
            try:
                result = await run_in_threadpool(
                    lambda: fetch(parsed.url, parsed.etag, parsed.last_modified)
                )
            except FeedFetchError as error:
                return _error(
                    502, error.code if error.code in _SAFE_FETCH_ERRORS else "fetch_failed"
                )
            except Exception:
                return _error(502, "fetch_failed")
        finally:
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

    return app
