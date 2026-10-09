"""Article API returns only bounded, inert extraction data."""

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.retrieval import api as retrieval_api
from primary_signal.retrieval.api import MAX_REQUEST_BYTES, create_retriever_app
from primary_signal.retrieval.article import ArticleFetchResult, RedirectHop
from primary_signal.retrieval.extract import ArticleExtraction

URL = "https://news.public.example/article"
NEXT = "https://news.public.example/story"
DIGEST = "a" * 64


def _result(status: int = 200) -> ArticleFetchResult:
    return ArticleFetchResult(
        status=status,
        final_url=NEXT,
        redirect_chain=(RedirectHop(301, NEXT),),
        content_type="text/html" if status == 200 else None,
        decoded_byte_count=32 if status == 200 else 0,
        raw_response_hash=DIGEST if status == 200 else None,
        extraction=ArticleExtraction("A title", "First paragraph.", 2, DIGEST)
        if status == 200
        else None,
        etag='"new"',
        last_modified=None,
    )


def _send(
    fetch: Callable[[str, str | None, str | None], ArticleFetchResult], **kwargs: Any
) -> Response:
    app = create_retriever_app(fetch_article=fetch)

    async def send() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            return await client.post("/v1/articles/fetch", **kwargs)

    return asyncio.run(send())


def test_article_route_serializes_flat_text_only_contract() -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    def fetch(url: str, etag: str | None, modified: str | None) -> ArticleFetchResult:
        seen.append((url, etag, modified))
        return _result()

    response = _send(fetch, json={"url": URL, "etag": '"old"'})
    assert response.status_code == 200
    assert response.json() == {
        "status": 200,
        "final_url": NEXT,
        "redirect_chain": [{"status": 301, "url": NEXT}],
        "content_type": "text/html",
        "decoded_byte_count": 32,
        "raw_response_hash": DIGEST,
        "extracted_title": "A title",
        "extracted_text": "First paragraph.",
        "word_count": 2,
        "normalized_content_hash": DIGEST,
        "extractor_name": "primary_signal_html",
        "extractor_version": "1",
        "normalization_version": 1,
        "etag": '"new"',
        "last_modified": None,
    }
    assert seen == [(URL, '"old"', None)]
    assert "body_base64" not in response.json()


def test_article_304_has_null_extraction_fields() -> None:
    def fetch(_url: str, _etag: str | None, _modified: str | None) -> ArticleFetchResult:
        return _result(304)

    response = _send(fetch, json={"url": URL})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == 304
    for key in (
        "raw_response_hash",
        "extracted_title",
        "extracted_text",
        "word_count",
        "normalized_content_hash",
        "extractor_name",
        "extractor_version",
        "normalization_version",
    ):
        assert data[key] is None


def test_article_route_rejects_malformed_and_oversized_request() -> None:
    def forbidden(*_: str | None) -> ArticleFetchResult:
        raise AssertionError("invalid request reached fetcher")

    for kwargs in (
        {"json": {"url": URL, "extra": 1}},
        {"json": {"url": 5}},
        {"content": b"{", "headers": {"content-type": "application/json"}},
        {"content": b"{}", "headers": {"content-type": "text/plain"}},
        {
            "content": b"x" * (MAX_REQUEST_BYTES + 1),
            "headers": {"content-type": "application/json"},
        },
    ):
        response = _send(forbidden, **kwargs)
        assert response.status_code in (400, 413)


def test_article_route_rejects_bad_content_length_and_stream_limit() -> None:
    def forbidden(*_: str | None) -> ArticleFetchResult:
        raise AssertionError("invalid request reached fetcher")

    for value, status in (("nope", 400), (str(MAX_REQUEST_BYTES + 1), 413)):
        response = _send(
            forbidden,
            content=b"{}",
            headers={"content-type": "application/json", "content-length": value},
        )
        assert response.status_code == status

    async def oversized() -> Any:
        yield b'{"url":"'
        yield b"x" * MAX_REQUEST_BYTES
        raise AssertionError("reader must stop at oversized chunk")

    response = _send(
        forbidden,
        content=oversized(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}


def test_article_route_maps_only_safe_error_codes() -> None:
    def unsafe(*_: str | None) -> ArticleFetchResult:
        raise FeedFetchError("private endpoint detail")

    response = _send(unsafe, json={"url": URL})
    assert response.status_code == 502
    assert response.json() == {"error": "fetch_failed"}

    def expected(*_: str | None) -> ArticleFetchResult:
        raise FeedFetchError("unsupported_charset")

    assert _send(expected, json={"url": URL}).json() == {"error": "unsupported_charset"}

    def unexpected(*_: str | None) -> ArticleFetchResult:
        raise RuntimeError("private endpoint detail")

    assert _send(unexpected, json={"url": URL}).json() == {"error": "fetch_failed"}


def test_default_callbacks_use_injected_http_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    def article(url: str, *, etag: str | None, last_modified: str | None) -> ArticleFetchResult:
        assert (url, etag, last_modified) == (URL, '"old"', None)
        return _result()

    def feed(url: str, *, etag: str | None, last_modified: str | None) -> FeedFetchResult:
        assert (url, etag, last_modified) == (URL, None, None)
        return FeedFetchResult(304, URL, b"")

    monkeypatch.setattr(retrieval_api, "retrieve_article", article)
    monkeypatch.setattr(retrieval_api, "fetch_feed", feed)
    app = create_retriever_app()

    async def send() -> tuple[Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            article_response = await client.post(
                "/v1/articles/fetch", json={"url": URL, "etag": '"old"'}
            )
            feed_response = await client.post("/v1/feeds/fetch", json={"url": URL})
            return article_response, feed_response

    article_response, feed_response = asyncio.run(send())
    assert article_response.json()["status"] == 200
    assert feed_response.json()["status"] == 304


def test_retriever_requires_positive_concurrency() -> None:
    with pytest.raises(ValueError, match="positive"):
        create_retriever_app(max_concurrent_fetches=0)


def test_feed_route_guards_request_length_and_unexpected_failure() -> None:
    def unexpected(*_: str | None) -> FeedFetchResult:
        raise RuntimeError("private detail")

    app = create_retriever_app(fetch=unexpected)

    async def send() -> tuple[Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            too_large = await client.post(
                "/v1/feeds/fetch",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-length": str(MAX_REQUEST_BYTES + 1),
                },
            )
            bad_length = await client.post(
                "/v1/feeds/fetch",
                content=b"{}",
                headers={"content-type": "application/json", "content-length": "bad"},
            )
            failure = await client.post("/v1/feeds/fetch", json={"url": URL})
            return too_large, bad_length, failure

    too_large, bad_length, failure = asyncio.run(send())
    assert too_large.json() == {"error": "request_too_large"}
    assert bad_length.json() == {"error": "invalid_request"}
    assert failure.json() == {"error": "fetch_failed"}


def test_article_and_feed_routes_share_concurrency_cap() -> None:
    entered = threading.Event()
    release = threading.Event()

    def fetch(*_: str | None) -> ArticleFetchResult:
        entered.set()
        assert release.wait(5)
        return _result()

    app = create_retriever_app(fetch_article=fetch, max_concurrent_fetches=1)

    async def send() -> tuple[Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first = asyncio.create_task(client.post("/v1/articles/fetch", json={"url": URL}))
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
