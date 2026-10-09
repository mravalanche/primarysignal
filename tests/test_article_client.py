"""The article client accepts only the fixed, bounded retriever protocol."""

from collections.abc import Callable

import httpx
import pytest

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.retrieval.client import MAX_API_RESPONSE_BYTES, RetrieverClient

URL = "https://news.public.example/article"
NEXT = "https://news.public.example/story"
DIGEST = "a" * 64


def _payload(status: int = 200) -> dict[str, object]:
    return {
        "status": status,
        "final_url": NEXT,
        "redirect_chain": [{"status": 301, "url": NEXT}],
        "content_type": "text/html" if status == 200 else None,
        "decoded_byte_count": 32 if status == 200 else 0,
        "raw_response_hash": DIGEST if status == 200 else None,
        "extracted_title": "A title" if status == 200 else None,
        "extracted_text": "First paragraph." if status == 200 else None,
        "word_count": 2 if status == 200 else None,
        "normalized_content_hash": DIGEST if status == 200 else None,
        "extractor_name": "primary_signal_html" if status == 200 else None,
        "extractor_version": "1" if status == 200 else None,
        "normalization_version": 1 if status == 200 else None,
        "etag": '"new"',
        "last_modified": None,
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> RetrieverClient:
    def streamed(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(response.content),
        )

    return RetrieverClient(
        "http://retriever.example.test:8080", transport=httpx.MockTransport(streamed)
    )


def test_fetch_article_uses_fixed_path_and_returns_text_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://retriever.example.test:8080/v1/articles/fetch"
        assert request.method == "POST"
        assert request.headers["accept-encoding"] == "identity"
        assert request.read() == (
            b'{"url":"https://news.public.example/article","etag":"\\"old\\"","last_modified":null}'
        )
        return httpx.Response(200, json=_payload())

    with _client(handler) as client:
        result = client.fetch_article(URL, '"old"')
    assert result.status == 200
    assert result.final_url == NEXT
    assert result.redirect_chain[0].url == NEXT
    assert result.extraction is not None
    assert result.extraction.text == "First paragraph."
    assert not hasattr(result, "body")


def test_fetch_article_accepts_304_without_extraction() -> None:
    with _client(lambda _: httpx.Response(200, json=_payload(304))) as client:
        result = client.fetch_article(URL)
    assert result.status == 304
    assert result.extraction is None
    assert result.decoded_byte_count == 0


@pytest.mark.parametrize(
    "change",
    [
        {"status": True},
        {"redirect_chain": [{"status": 200, "url": NEXT}]},
        {"redirect_chain": [{"status": 301, "url": "http://127.0.0.1/private"}]},
        {"redirect_chain": [{"status": 301, "url": NEXT}] * 6},
        {"extracted_text": "x" * 200_001},
        {"normalized_content_hash": "not-a-hash"},
        {"extractor_version": "unexpected"},
        {"normalization_version": True},
        {"word_count": -1},
        {"decoded_byte_count": True},
        {"extracted_text": None},
        {"content_type": 5},
        {"etag": "x" * 8193},
        {"redirect_chain": ["not-an-object"]},
        {"redirect_chain": [{"status": 301, "url": NEXT, "extra": 1}]},
        {"redirect_chain": [{"status": True, "url": NEXT}]},
        {"redirect_chain": [{"status": 301, "url": 5}]},
        {"final_url": "x" * 8193},
        {"redirect_chain": "not-a-list"},
    ],
)
def test_fetch_article_rejects_malformed_success(change: dict[str, object]) -> None:
    payload = _payload() | change
    with (
        _client(lambda _: httpx.Response(200, json=payload)) as client,
        pytest.raises(FeedFetchError, match="retriever_protocol"),
    ):
        client.fetch_article(URL)


def test_fetch_article_rejects_unknown_and_duplicate_fields() -> None:
    with (
        _client(lambda _: httpx.Response(200, json=_payload() | {"unexpected": 1})) as client,
        pytest.raises(FeedFetchError, match="retriever_protocol"),
    ):
        client.fetch_article(URL)
    with (
        _client(lambda _: httpx.Response(200, content=b'{"status":200,"status":304}')) as client,
        pytest.raises(FeedFetchError, match="retriever_protocol"),
    ):
        client.fetch_article(URL)


def test_feed_eligible_long_article_url_can_cross_client_boundary() -> None:
    article_url = "https://news.public.example/" + "a" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        assert article_url in request.read().decode("utf-8")
        return httpx.Response(
            200,
            json=_payload()
            | {
                "final_url": article_url,
                "redirect_chain": [],
            },
        )

    with _client(handler) as client:
        result = client.fetch_article(article_url)
    assert result.final_url == article_url


def test_fetch_article_rejects_304_with_content() -> None:
    payload = _payload(304) | {"extracted_text": "unexpected"}
    with (
        _client(lambda _: httpx.Response(200, json=payload)) as client,
        pytest.raises(FeedFetchError, match="retriever_protocol"),
    ):
        client.fetch_article(URL)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(302, headers={"location": URL}), "retriever_protocol"),
        (httpx.Response(502, json={"error": "fetch_timeout"}), "dependency_timeout"),
        (httpx.Response(502, json={"error": "unsupported_charset"}), "unsupported_charset"),
        (httpx.Response(503, json={"error": "retriever_busy"}), "rate_limited"),
        (httpx.Response(429, json={"error": "private detail"}), "rate_limited"),
        (httpx.Response(502, json={"error": "secret detail"}), "dependency_unavailable"),
        (httpx.Response(200, headers={"content-length": "invalid"}), "retriever_protocol"),
        (httpx.Response(200, content=b"x" * (MAX_API_RESPONSE_BYTES + 1)), "retriever_protocol"),
    ],
)
def test_fetch_article_maps_errors_and_caps_response(response: httpx.Response, code: str) -> None:
    with _client(lambda _: response) as client, pytest.raises(FeedFetchError, match=code):
        client.fetch_article(URL)


def test_fetch_article_caps_unannounced_stream_size() -> None:
    oversized = b"x" * (MAX_API_RESPONSE_BYTES + 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(oversized))

    with (
        RetrieverClient(
            "http://retriever.example.test:8080", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(FeedFetchError, match="retriever_protocol"),
    ):
        client.fetch_article(URL)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("synthetic timeout"), "dependency_timeout"),
        (httpx.ConnectError("synthetic unavailable"), "dependency_unavailable"),
    ],
)
def test_fetch_article_maps_transport_failures(error: Exception, code: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    with _client(handler) as client, pytest.raises(FeedFetchError, match=code):
        client.fetch_article(URL)
