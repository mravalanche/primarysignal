"""Synthetic tests for the retriever's narrow internal HTTP contract."""

import asyncio
import base64
import threading
from collections.abc import Callable
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.retrieval.api import MAX_REQUEST_BYTES, create_retriever_app

URL = "https://feed.public.example/news.xml"


def _send(
    fetch: Callable[[str, str | None, str | None], FeedFetchResult], **kwargs: Any
) -> Response:
    app = create_retriever_app(fetch=fetch)

    async def send() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            return await client.post("/v1/feeds/fetch", **kwargs)

    return asyncio.run(send())


def test_retrieval_round_trip_and_conditional_validators() -> None:
    observed: list[tuple[str, str | None, str | None]] = []

    def fetch(url: str, etag: str | None, last_modified: str | None) -> FeedFetchResult:
        observed.append((url, etag, last_modified))
        return FeedFetchResult(200, URL, b"<feed/>", '"next"', None)

    response = _send(fetch, json={"url": URL, "etag": '"old"', "last_modified": None})
    assert response.status_code == 200
    assert response.json() == {
        "status": 200,
        "final_url": URL,
        "body_base64": base64.b64encode(b"<feed/>").decode(),
        "etag": '"next"',
        "last_modified": None,
    }
    assert observed == [(URL, '"old"', None)]


def test_304_returns_empty_body() -> None:
    response = _send(
        lambda _url, _etag, _last_modified: FeedFetchResult(304, URL, b""),
        json={"url": URL},
    )
    assert response.status_code == 200
    assert response.json()["body_base64"] == ""


def test_rejects_unknown_or_wrongly_typed_fields_without_fetching() -> None:
    def forbidden(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        raise AssertionError("invalid request reached fetcher")

    for payload in (
        {"url": URL, "other": "unexpected"},
        {"url": 5},
        {"url": URL, "etag": 5},
        {"etag": '"old"'},
        [URL],
    ):
        response = _send(forbidden, json=payload)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_request"}


def test_rejects_bad_content_type_and_malformed_json() -> None:
    def forbidden(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        raise AssertionError("invalid request reached fetcher")

    for kwargs in (
        {"content": b"{}", "headers": {"content-type": "text/plain"}},
        {"content": b"{", "headers": {"content-type": "application/json"}},
    ):
        response = _send(forbidden, **kwargs)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_request"}


def test_rejects_oversize_stream_before_fetching() -> None:
    calls = 0

    def forbidden(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        nonlocal calls
        calls += 1
        raise AssertionError("oversize request reached fetcher")

    async def oversized() -> Any:
        yield b'{"url":"'
        yield b"x" * MAX_REQUEST_BYTES
        raise AssertionError("reader must stop at the first oversized chunk")

    response = _send(forbidden, content=oversized(), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}
    assert calls == 0


def test_fetch_failure_uses_safe_error_only() -> None:
    def expected(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        raise FeedFetchError("fetch_timeout")

    response = _send(expected, json={"url": URL})
    assert response.status_code == 502
    assert response.json() == {"error": "fetch_timeout"}

    def policy(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        raise FeedFetchError("forbidden_address")

    response = _send(policy, json={"url": URL})
    assert response.status_code == 502
    assert response.json() == {"error": "forbidden_address"}

    def unsafe(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        raise FeedFetchError("private URL or credential")

    response = _send(unsafe, json={"url": URL})
    assert response.status_code == 502
    assert response.json() == {"error": "fetch_failed"}


def test_global_concurrency_cap_rejects_second_fetch() -> None:
    entered = threading.Event()
    release = threading.Event()

    def fetch(_url: str, _etag: str | None, _last_modified: str | None) -> FeedFetchResult:
        entered.set()
        assert release.wait(5)
        return FeedFetchResult(200, URL, b"ok")

    app = create_retriever_app(fetch=fetch, max_concurrent_fetches=1)

    async def send() -> tuple[Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = asyncio.create_task(client.post("/v1/feeds/fetch", json={"url": URL}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                second = await client.post("/v1/feeds/fetch", json={"url": URL})
            finally:
                release.set()
            return await first, second

    first, second = asyncio.run(send())
    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json() == {"error": "retriever_busy"}
