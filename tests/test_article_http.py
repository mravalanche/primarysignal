"""Article retrieval keeps hostile HTML inside the bounded retriever."""

import gzip
import hashlib
import io
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.retrieval.article import ArticleFetchResult, RedirectHop
from primary_signal.retrieval.extract import ArticleExtraction
from primary_signal.retrieval.http import fetch_article
from primary_signal.retrieval.policy import ValidatedTarget


class _Socket:
    def __init__(self, response: bytes, requests: list[bytes], peer: str = "8.8.8.8") -> None:
        self.response = response
        self.requests = requests
        self.peer = peer

    def getpeername(self) -> tuple[str, int]:
        return self.peer, 80

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def sendall(self, request: bytes) -> None:
        self.requests.append(request)

    def makefile(self, mode: str) -> Any:
        assert mode == "rb"
        return io.BytesIO(self.response)

    def close(self) -> None:
        pass


def _resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


def _reply(
    response: bytes, requests: list[bytes]
) -> Callable[
    [ValidatedTarget, ipaddress.IPv4Address | ipaddress.IPv6Address, float], socket.socket
]:
    def connect(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        return cast(socket.socket, _Socket(response, requests))

    return connect


def _html_response(body: bytes, *, content_type: bytes = b"text/html; charset=utf-8") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: "
        + content_type
        + b"\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\n\r\n"
        + body
    )


def test_article_fetch_returns_only_extracted_text_and_provenance() -> None:
    html = b"<html><head><title>Notice</title></head><body><article><h1>Update</h1><p>Patch now.</p><script>secret()</script></article></body></html>"
    requests: list[bytes] = []
    result = fetch_article(
        "http://news.public.example/notice",
        resolver=_resolver,
        connector=_reply(_html_response(html), requests),
    )

    assert result.status == 200
    assert result.final_url == "http://news.public.example/notice"
    assert result.redirect_chain == ()
    assert result.decoded_byte_count == len(html)
    assert result.raw_response_hash == hashlib.sha256(html).hexdigest()
    assert result.extraction is not None
    assert result.extraction.title == "Notice"
    assert result.extraction.text == "Update\nPatch now."
    assert "secret" not in result.extraction.text
    assert b"Accept: text/html, application/xhtml+xml\r\n" in requests[0]
    assert b"User-Agent: PrimarySignalArticleFetch/1\r\n" in requests[0]


def test_not_modified_has_no_extracted_content() -> None:
    requests: list[bytes] = []
    result = fetch_article(
        "http://news.public.example/notice",
        etag='"one"',
        resolver=_resolver,
        connector=_reply(b'HTTP/1.1 304 Not Modified\r\nETag: "two"\r\n\r\n', requests),
    )

    assert result.status == 304
    assert result.extraction is None
    assert result.raw_response_hash is None
    assert result.decoded_byte_count == 0
    assert result.etag == '"two"'
    assert b'If-None-Match: "one"\r\n' in requests[0]


def test_redirect_chain_is_validated_and_cross_origin_validator_is_cleared() -> None:
    requests: list[bytes] = []
    html = b"<main><p>New report.</p></main>"
    replies = iter(
        (
            b"HTTP/1.1 302 Found\r\nLocation: http://other.public.example/story\r\n\r\n",
            _html_response(html),
        )
    )

    def connect(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        return cast(socket.socket, _Socket(next(replies), requests))

    result = fetch_article(
        "http://news.public.example/notice",
        etag='"one"',
        resolver=_resolver,
        connector=connect,
    )

    assert [(hop.status, hop.url) for hop in result.redirect_chain] == [
        (302, "http://other.public.example/story")
    ]
    assert result.final_url == "http://other.public.example/story"
    assert b'If-None-Match: "one"\r\n' in requests[0]
    assert b"If-None-Match:" not in requests[1]


def test_private_redirect_destination_is_rejected_before_second_connection() -> None:
    calls: list[str] = []

    def resolve(host: str, _port: int) -> tuple[str, ...]:
        return ("127.0.0.1",) if host == "private.public.example" else ("8.8.8.8",)

    def connect(
        target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        calls.append(target.hostname)
        return cast(
            socket.socket,
            _Socket(
                b"HTTP/1.1 302 Found\r\nLocation: http://private.public.example/\r\n\r\n",
                [],
            ),
        )

    with pytest.raises(FeedFetchError, match="forbidden_address"):
        fetch_article("http://news.public.example/", resolver=resolve, connector=connect)
    assert calls == ["news.public.example"]


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (
            _html_response(b"<p>text</p>", content_type=b"application/pdf"),
            "unsupported_content_type",
        ),
        (
            _html_response(b"<p>text</p>", content_type=b"text/html; charset=iso-8859-1"),
            "unsupported_charset",
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n\r\n"
            + gzip.compress(b"<p>" + b"x" * (2 * 1024 * 1024) + b"</p>"),
            "response_too_large",
        ),
    ],
)
def test_article_response_rejects_unsupported_or_oversized_content(
    response: bytes, error_code: str
) -> None:
    with pytest.raises(FeedFetchError, match=error_code):
        fetch_article(
            "http://news.public.example/",
            resolver=_resolver,
            connector=_reply(response, []),
        )


def test_article_result_requires_redirect_provenance_and_no_body_on_304() -> None:
    with pytest.raises(ValueError, match="does not end at final URL"):
        ArticleFetchResult(
            status=200,
            final_url="https://news.public.example/final",
            redirect_chain=(RedirectHop(302, "https://news.public.example/other"),),
            content_type="text/html",
            decoded_byte_count=12,
            raw_response_hash="a" * 64,
            extraction=ArticleExtraction("Title", "Text", 1, "b" * 64),
            etag=None,
            last_modified=None,
        )
    with pytest.raises(ValueError, match="must not contain extracted content"):
        ArticleFetchResult(
            status=304,
            final_url="https://news.public.example/final",
            redirect_chain=(),
            content_type=None,
            decoded_byte_count=0,
            raw_response_hash="a" * 64,
            extraction=None,
            etag=None,
            last_modified=None,
        )


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (b"HTTP/1.1 302 Found\r\n\r\n", "invalid_redirect"),
        (b"HTTP/1.1 302 Found\r\nLocation: http://news.public.example/\r\n\r\n", "redirect_loop"),
        (b"HTTP/1.1 503 Unavailable\r\n\r\n", "http_status"),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: " + b"x" * 257 + b"\r\n\r\n",
            "invalid_response_headers",
        ),
        (
            _html_response(b"<p>text</p>", content_type=b"text/html; charset=utf-16"),
            "unsupported_charset",
        ),
    ],
)
def test_article_fetch_rejects_bad_status_redirect_and_headers(
    response: bytes, error_code: str
) -> None:
    with pytest.raises(FeedFetchError, match=error_code):
        fetch_article(
            "http://news.public.example/",
            resolver=_resolver,
            connector=_reply(response, []),
        )


def test_article_fetch_rejects_network_error() -> None:
    def connect(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        raise OSError("synthetic unavailable")

    with pytest.raises(FeedFetchError, match="fetch_network"):
        fetch_article("http://news.public.example/", resolver=_resolver, connector=connect)


def test_article_urls_share_the_client_length_limit() -> None:
    eligible_url = "http://news.public.example/" + "a" * 5000
    result = fetch_article(
        eligible_url,
        resolver=_resolver,
        connector=_reply(_html_response(b"<p>Long URL article.</p>"), []),
    )
    assert result.final_url == eligible_url

    long_url = "http://news.public.example/" + "a" * 8192
    with pytest.raises(FeedFetchError, match="url"):
        fetch_article(long_url, resolver=_resolver, connector=_reply(b"", []))

    redirect = b"HTTP/1.1 302 Found\r\nLocation: /" + b"a" * 8192 + b"\r\n\r\n"
    with pytest.raises(FeedFetchError, match="invalid_redirect"):
        fetch_article(
            "http://news.public.example/",
            resolver=_resolver,
            connector=_reply(redirect, []),
        )


def test_article_result_rejects_invalid_bounds() -> None:
    valid = ArticleFetchResult(
        status=200,
        final_url="https://news.public.example/final",
        redirect_chain=(),
        content_type="text/html",
        decoded_byte_count=12,
        raw_response_hash="a" * 64,
        extraction=ArticleExtraction("Title", "Text", 1, "b" * 64),
        etag=None,
        last_modified=None,
    )
    invalid = (
        ({"status": 201}, "must be HTTP 200 or 304"),
        ({"final_url": "https://news.public.example/" + "a" * 8192}, "final URL exceeds its bound"),
        (
            {"redirect_chain": (RedirectHop(302, "https://news.public.example/final"),) * 6},
            "exceeds its bound",
        ),
        ({"content_type": "text/html\r\nInjected"}, "invalid article content type"),
        ({"etag": "bad\r\nInjected"}, "invalid article validator"),
        ({"decoded_byte_count": -1}, "byte count exceeds its bound"),
        ({"raw_response_hash": "bad"}, "requires extracted content"),
    )
    for replacement, message in invalid:
        with pytest.raises(ValueError, match=message):
            replace(valid, **replacement)
    with pytest.raises(ValueError, match="redirect URL exceeds its bound"):
        RedirectHop(302, "https://news.public.example/" + "a" * 8192)
