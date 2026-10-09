"""Synthetic tests for the retriever's narrow internal HTTP contract."""

import asyncio
import base64
import threading
from collections.abc import Callable
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.retrieval import api as retrieval_api
from primary_signal.retrieval.api import MAX_REQUEST_BYTES, create_retriever_app
from primary_signal.retrieval.article import ArticleFetchResult

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


def test_feed_and_article_share_canonical_hostname_rate_limit() -> None:
    feed_calls = 0
    article_calls = 0

    def feed(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        nonlocal feed_calls
        feed_calls += 1
        return FeedFetchResult(304, url, b"")

    def article(url: str, _etag: str | None, _modified: str | None) -> ArticleFetchResult:
        nonlocal article_calls
        article_calls += 1
        return ArticleFetchResult(304, url, (), None, 0, None, None, None, None)

    app = create_retriever_app(fetch=feed, fetch_article=article, max_requests_per_origin=2)

    async def send() -> tuple[Response, Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = await client.post("/v1/feeds/fetch", json={"url": URL})
            second = await client.post(
                "/v1/articles/fetch", json={"url": "https://FEED.PUBLIC.EXAMPLE:443/story"}
            )
            limited = await client.post("/v1/feeds/fetch", json={"url": URL})
            other = await client.post(
                "/v1/articles/fetch", json={"url": "https://other.public.example/story"}
            )
            return first, second, limited, other

    first, second, limited, other = asyncio.run(send())
    assert [response.status_code for response in (first, second, limited, other)] == [
        200,
        200,
        429,
        200,
    ]
    assert limited.json() == {"error": "origin_rate_limited"}
    assert (feed_calls, article_calls) == (1, 2)


def test_unicode_and_idna_host_forms_share_one_bucket() -> None:
    def fetch(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        return FeedFetchResult(304, url, b"")

    app = create_retriever_app(fetch=fetch, max_requests_per_origin=1)

    async def send() -> tuple[Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = await client.post(
                "/v1/feeds/fetch", json={"url": "https://bücher.public.example/feed"}
            )
            second = await client.post(
                "/v1/feeds/fetch", json={"url": "https://xn--bcher-kva.public.example/feed"}
            )
            return first, second

    first, second = asyncio.run(send())
    assert (first.status_code, second.status_code) == (200, 429)


def test_per_origin_concurrency_is_shared_across_routes() -> None:
    entered = threading.Event()
    release = threading.Event()
    article_calls = 0

    def feed(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        entered.set()
        assert release.wait(5)
        return FeedFetchResult(304, url, b"")

    def article(url: str, _etag: str | None, _modified: str | None) -> ArticleFetchResult:
        nonlocal article_calls
        article_calls += 1
        return ArticleFetchResult(304, url, (), None, 0, None, None, None, None)

    app = create_retriever_app(fetch=feed, fetch_article=article, max_concurrent_per_origin=1)

    async def send() -> tuple[Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = asyncio.create_task(client.post("/v1/feeds/fetch", json={"url": URL}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                same = await client.post("/v1/articles/fetch", json={"url": URL})
                other = await client.post(
                    "/v1/articles/fetch", json={"url": "https://other.public.example/story"}
                )
            finally:
                release.set()
            return await first, same, other

    first, same, other = asyncio.run(send())
    assert (first.status_code, same.status_code, other.status_code) == (200, 503, 200)
    assert same.json() == {"error": "retriever_busy"}
    assert article_calls == 1


def test_origin_state_is_bounded_and_expires_without_real_time_sleep(
    monkeypatch: Any,
) -> None:
    now = [100.0]
    monkeypatch.setattr(retrieval_api, "monotonic", lambda: now[0])

    def fetch(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        return FeedFetchResult(304, url, b"")

    app = create_retriever_app(
        fetch=fetch,
        max_tracked_origins=1,
        max_requests_per_origin=1,
        origin_window_seconds=60,
    )

    async def send() -> tuple[Response, Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = await client.post("/v1/feeds/fetch", json={"url": URL})
            blocked = await client.post(
                "/v1/feeds/fetch", json={"url": "https://other.public.example/feed"}
            )
            now[0] += 61
            after_expiry = await client.post(
                "/v1/feeds/fetch", json={"url": "https://other.public.example/feed"}
            )
            old_origin = await client.post("/v1/feeds/fetch", json={"url": URL})
            return first, blocked, after_expiry, old_origin

    first, blocked, after_expiry, old_origin = asyncio.run(send())
    assert first.status_code == 200
    assert blocked.status_code == 503
    assert blocked.json() == {"error": "retriever_busy"}
    assert after_expiry.status_code == 200
    assert old_origin.status_code == 503


def test_origin_rate_refills_without_resetting_the_app(monkeypatch: Any) -> None:
    now = [100.0]
    monkeypatch.setattr(retrieval_api, "monotonic", lambda: now[0])

    def fetch(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        return FeedFetchResult(304, url, b"")

    app = create_retriever_app(fetch=fetch, max_requests_per_origin=1, origin_window_seconds=60)

    async def send() -> tuple[Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = await client.post("/v1/feeds/fetch", json={"url": URL})
            limited = await client.post("/v1/feeds/fetch", json={"url": URL})
            now[0] += 60
            refilled = await client.post("/v1/feeds/fetch", json={"url": URL})
            return first, limited, refilled

    first, limited, refilled = asyncio.run(send())
    assert (first.status_code, limited.status_code, refilled.status_code) == (200, 429, 200)


def test_initial_host_is_charged_when_fetcher_follows_a_redirect() -> None:
    other_url = "https://other.public.example/story"

    def fetch(_url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        return FeedFetchResult(304, other_url, b"")

    app = create_retriever_app(fetch=fetch, max_requests_per_origin=1)

    async def send() -> tuple[Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = await client.post("/v1/feeds/fetch", json={"url": URL})
            first_origin = await client.post("/v1/feeds/fetch", json={"url": URL})
            destination_origin = await client.post("/v1/feeds/fetch", json={"url": other_url})
            return first, first_origin, destination_origin

    first, first_origin, destination_origin = asyncio.run(send())
    assert (first.status_code, first_origin.status_code, destination_origin.status_code) == (
        200,
        429,
        200,
    )


def test_origin_key_rejects_unsafe_authorities_before_fetching() -> None:
    def forbidden(_url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        raise AssertionError("invalid URL reached fetcher")

    for url, code in (
        ("https://user@feed.public.example/news.xml", "authority"),
        ("https://127.0.0.1:8080/news.xml", "port"),
        ("file:///etc/passwd", "scheme"),
    ):
        response = _send(forbidden, json={"url": url})
        assert response.status_code == 502
        assert response.json() == {"error": code}
