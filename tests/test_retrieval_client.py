"""The processor talks only to the bounded internal retriever API."""

import base64
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedPollTarget
from primary_signal.retrieval import client as retrieval_client
from primary_signal.retrieval.client import MAX_API_RESPONSE_BYTES, RetrieverClient


def _stream(response: httpx.Response) -> httpx.Response:
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        stream=httpx.ByteStream(response.content),
    )


def _target() -> FeedPollTarget:
    return FeedPollTarget(
        feed_id=uuid.uuid4(),
        url="https://feed.public.example/rss.xml",
        etag='"one"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
    )


def _fetch_with(handler: Callable[[httpx.Request], httpx.Response]) -> bytes:
    with RetrieverClient(
        "http://retriever.example.test:8080", transport=httpx.MockTransport(handler)
    ) as fetcher:
        return fetcher.fetch(_target()).body


def test_fetch_uses_only_fixed_internal_path_and_explicit_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://retriever.example.test:8080/v1/feeds/fetch"
        assert request.method == "POST"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["accept"] == "application/json"
        assert request.read() == (
            b'{"url":"https://feed.public.example/rss.xml","etag":"\\"one\\"",'
            b'"last_modified":"Wed, 01 Jan 2025 00:00:00 GMT"}'
        )
        return _stream(
            httpx.Response(
                200,
                json={
                    "status": 200,
                    "final_url": "https://feed.public.example/rss.xml",
                    "body_base64": base64.b64encode(b"<rss/>").decode("ascii"),
                    "etag": None,
                    "last_modified": None,
                },
            )
        )

    assert _fetch_with(handler) == b"<rss/>"


def test_304_response_has_empty_body() -> None:
    assert (
        _fetch_with(
            lambda _request: _stream(
                httpx.Response(
                    200,
                    json={
                        "status": 304,
                        "final_url": "https://feed.public.example/rss.xml",
                        "body_base64": "",
                        "etag": '"one"',
                        "last_modified": None,
                    },
                )
            )
        )
        == b""
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:REPLACE_ME@host",  # pragma: allowlist secret
        "https://host/path",
        "ftp://host",
        "https://host?q=1",
    ],
)
def test_base_url_must_be_a_fixed_origin(origin: str) -> None:
    with pytest.raises(ValueError, match="fixed HTTP origin"):
        RetrieverClient(origin)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            httpx.Response(302, headers={"location": "https://feed.public.example"}),
            "retriever_protocol",
        ),
        (httpx.Response(502, json={"error": "dns_error"}), "dependency_unavailable"),
        (httpx.Response(502, json={"error": "fetch_network"}), "dependency_unavailable"),
        (httpx.Response(502, json={"error": "fetch_failed"}), "dependency_unavailable"),
        (httpx.Response(502, json={"error": "fetch_timeout"}), "dependency_timeout"),
        (httpx.Response(503, json={"error": "retriever_busy"}), "rate_limited"),
        (httpx.Response(429, content=b"bad gateway body"), "rate_limited"),
        (httpx.Response(503, content=b"bad gateway body"), "dependency_unavailable"),
        (httpx.Response(503, json={"error": "injected_secret"}), "dependency_unavailable"),
        (httpx.Response(500, json={"error": "injected_secret"}), "dependency_unavailable"),
        (httpx.Response(502, json={"error": "forbidden_address"}), "forbidden_address"),
        (httpx.Response(400, json={"error": "injected_secret"}), "retriever_protocol"),
        (httpx.Response(200, content=b'{"status":200,"status":304}'), "retriever_protocol"),
        (httpx.Response(200, content=b"[1,2]"), "retriever_protocol"),
        (
            httpx.Response(200, headers={"content-length": str(MAX_API_RESPONSE_BYTES + 1)}),
            "retriever_protocol",
        ),
        (httpx.Response(200, content=b"x" * (MAX_API_RESPONSE_BYTES + 1)), "retriever_protocol"),
    ],
)
def test_rejects_protocol_failures(response: httpx.Response, code: str) -> None:
    with (
        RetrieverClient(
            "http://retriever.example.test",
            transport=httpx.MockTransport(lambda _request: _stream(response)),
        ) as fetcher,
        pytest.raises(FeedFetchError, match=code),
    ):
        fetcher.fetch(_target())


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": 201,
            "final_url": "https://feed.public.example",
            "body_base64": "",
            "etag": None,
            "last_modified": None,
        },
        {
            "status": True,
            "final_url": "https://feed.public.example",
            "body_base64": "",
            "etag": None,
            "last_modified": None,
        },
        {
            "status": 304,
            "final_url": "https://feed.public.example",
            "body_base64": "eA==",
            "etag": None,
            "last_modified": None,
        },
        {
            "status": 200,
            "final_url": "https://feed.public.example",
            "body_base64": "!",
            "etag": None,
            "last_modified": None,
        },
    ],
)
def test_rejects_invalid_success(payload: dict[str, object]) -> None:
    with (
        RetrieverClient(
            "http://retriever.example.test",
            transport=httpx.MockTransport(
                lambda _request: _stream(httpx.Response(200, json=payload))
            ),
        ) as fetcher,
        pytest.raises(FeedFetchError, match="retriever_protocol"),
    ):
        fetcher.fetch(_target())


def test_transport_error_uses_stable_code() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive endpoint")

    with (
        RetrieverClient(
            "http://retriever.example.test", transport=httpx.MockTransport(unavailable)
        ) as fetcher,
        pytest.raises(FeedFetchError, match="dependency_unavailable"),
    ):
        fetcher.fetch(_target())


def test_transport_timeout_is_retryable() -> None:
    def timed_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive endpoint")

    with (
        RetrieverClient(
            "http://retriever.example.test", transport=httpx.MockTransport(timed_out)
        ) as fetcher,
        pytest.raises(FeedFetchError, match="dependency_timeout"),
    ):
        fetcher.fetch(_target())


def test_drip_response_hits_total_deadline_before_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(retrieval_client, "monotonic", lambda: now[0])

    class Drip(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(4):
                now[0] += 9.0
                yield b"x"

    with (
        RetrieverClient(
            "http://retriever.example.test",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=Drip())),
        ) as fetcher,
        pytest.raises(FeedFetchError, match="dependency_timeout"),
    ):
        fetcher.fetch(_target())
