"""Synthetic transport tests for bounded feed HTTP retrieval."""

import gzip
import io
import ipaddress
import socket
import time
import zlib
from collections.abc import Callable
from typing import Any

import pytest

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.retrieval import http as retrieval_http
from primary_signal.retrieval.dns import DnsCapacityError
from primary_signal.retrieval.http import fetch_feed
from primary_signal.retrieval.policy import ValidatedTarget


class _FakeSocket:
    def __init__(self, response: bytes, peer: str, requests: list[bytes] | None) -> None:
        self.response = response
        self.peer = peer
        self.requests = requests

    def getpeername(self) -> tuple[str, int]:
        return self.peer, 443

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def sendall(self, data: bytes) -> None:
        if self.requests is not None:
            self.requests.append(data)

    def makefile(self, mode: str) -> Any:
        assert mode == "rb"
        return io.BytesIO(self.response)

    def close(self) -> None:
        pass


def _reply(
    response: bytes,
    *,
    peer: str = "8.8.8.8",
    requests: list[bytes] | None = None,
) -> Callable[
    [ValidatedTarget, ipaddress.IPv4Address | ipaddress.IPv6Address, float], socket.socket
]:
    def connector(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        return _FakeSocket(response, peer, requests)  # type: ignore[return-value]

    return connector


def _resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


@pytest.mark.parametrize(
    ("address", "family", "sockaddr"),
    [
        ("8.8.8.8", socket.AF_INET, ("8.8.8.8", 80)),
        ("2606:4700:4700::1111", socket.AF_INET6, ("2606:4700:4700::1111", 80, 0, 0)),
    ],
)
def test_numeric_connect_never_resolves_again(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
    family: int,
    sockaddr: tuple[object, ...],
) -> None:
    connected: list[tuple[object, ...]] = []

    class Socket(_FakeSocket):
        def __init__(self) -> None:
            super().__init__(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", address, None)

        def connect(self, target: tuple[object, ...]) -> None:
            connected.append(target)

    def make_socket(actual_family: int, kind: int) -> socket.socket:
        assert actual_family == family and kind == socket.SOCK_STREAM
        return Socket()  # type: ignore[return-value]

    def fail_dns(*_args: object, **_kwargs: object) -> None:
        pytest.fail("numeric connection must not resolve DNS")

    monkeypatch.setattr(retrieval_http.socket, "socket", make_socket)
    monkeypatch.setattr(retrieval_http.socket, "getaddrinfo", fail_dns)
    url = f"http://[{address}]/" if ":" in address else f"http://{address}/"
    result = fetch_feed(url)
    assert result.status == 200
    assert connected == [sockaddr]


def test_fetch_feed_sends_conditional_headers_without_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8888")
    requests: list[bytes] = []
    result = fetch_feed(
        "http://feed.public.example/path?q=1",
        etag='"safe"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        resolver=_resolver,
        connector=_reply(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ntest", requests=requests),
    )
    assert result.status == 200 and result.body == b"test"
    assert b"GET /path?q=1 HTTP/1.1\r\n" in requests[0]
    assert b'If-None-Match: "safe"\r\n' in requests[0]
    assert b"If-Modified-Since: Wed, 01 Jan 2025 00:00:00 GMT\r\n" in requests[0]
    assert b"127.0.0.1" not in requests[0]


def test_peer_address_must_match_validated_dns_answer() -> None:
    with pytest.raises(FeedFetchError, match="peer_mismatch"):
        fetch_feed(
            "http://feed.public.example/",
            resolver=_resolver,
            connector=_reply(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", peer="1.1.1.1"),
        )


def test_dns_capacity_failure_never_connects() -> None:
    def saturated(_host: str, _port: int) -> tuple[str, ...]:
        raise DnsCapacityError("dns_capacity")

    def connector(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        pytest.fail("a failed DNS lookup must not start a connection")

    with pytest.raises(FeedFetchError, match="dns_error"):
        fetch_feed("http://feed.public.example/", resolver=saturated, connector=connector)


def test_rejects_oversized_compressed_or_decompressed_body() -> None:
    oversized = b"HTTP/1.1 200 OK\r\nContent-Length: 2097153\r\n\r\n"
    with pytest.raises(FeedFetchError, match="response_too_large"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(oversized))
    bomb = gzip.compress(b"a" * (2 * 1024 * 1024 + 1))
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
        + str(len(bomb)).encode()
        + b"\r\n\r\n"
        + bomb
    )
    with pytest.raises(FeedFetchError, match="response_too_large"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


def test_http_304_has_no_body() -> None:
    result = fetch_feed(
        "http://feed.public.example/",
        resolver=_resolver,
        connector=_reply(b'HTTP/1.1 304 Not Modified\r\nETag: "next"\r\n\r\n'),
    )
    assert result.status == 304 and result.body == b"" and result.etag == '"next"'


def test_redirect_revalidates_and_clears_cross_origin_validator() -> None:
    requests: list[bytes] = []
    replies = iter(
        (
            b"HTTP/1.1 302 Found\r\nLocation: http://other.public.example/next\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        )
    )

    def connector(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        return _FakeSocket(next(replies), "8.8.8.8", requests)  # type: ignore[return-value]

    result = fetch_feed(
        "http://feed.public.example/",
        etag='"private"',
        resolver=_resolver,
        connector=connector,
    )
    assert result.final_url == "http://other.public.example/next"
    assert b'If-None-Match: "private"' in requests[0]
    assert b"If-None-Match:" not in requests[1]


def test_https_downgrade_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class Context:
        def wrap_socket(self, connection: socket.socket, *, server_hostname: str) -> socket.socket:
            assert server_hostname == "feed.public.example"
            return connection

    monkeypatch.setattr(retrieval_http.ssl, "create_default_context", Context)
    with pytest.raises(FeedFetchError, match="https_downgrade"):
        fetch_feed(
            "https://feed.public.example/",
            resolver=_resolver,
            connector=_reply(
                b"HTTP/1.1 302 Found\r\nLocation: http://other.public.example/\r\n\r\n"
            ),
        )


def test_upgrade_redirect_clears_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    class Context:
        def wrap_socket(self, connection: socket.socket, *, server_hostname: str) -> socket.socket:
            assert server_hostname == "feed.public.example"
            return connection

    monkeypatch.setattr(retrieval_http.ssl, "create_default_context", Context)
    requests: list[bytes] = []
    replies = iter(
        (
            b"HTTP/1.1 301 Moved\r\nLocation: https://feed.public.example/next\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        )
    )

    def connector(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        return _FakeSocket(next(replies), "8.8.8.8", requests)  # type: ignore[return-value]

    result = fetch_feed(
        "http://feed.public.example/", etag='"previous"', resolver=_resolver, connector=connector
    )
    assert result.status == 200
    assert b'If-None-Match: "previous"' in requests[0]
    assert b"If-None-Match:" not in requests[1]


def test_redirect_dns_uses_remaining_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    class Resolver:
        def resolve(self, _hostname: str, _port: int, *, timeout_seconds: float) -> tuple[str, ...]:
            waits.append(timeout_seconds)
            return ("8.8.8.8",)

    monkeypatch.setattr(retrieval_http, "DEFAULT_RESOLVER", Resolver())
    monkeypatch.setattr(retrieval_http, "REQUEST_TIMEOUT_SECONDS", 0.1)
    replies = iter(
        (
            b"HTTP/1.1 302 Found\r\nLocation: http://other.public.example/\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
        )
    )

    def connector(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        if not waits or len(waits) == 1:
            time.sleep(0.03)
        return _FakeSocket(next(replies), "8.8.8.8", None)  # type: ignore[return-value]

    result = fetch_feed("http://feed.public.example/", connector=connector)
    assert result.status == 200
    assert len(waits) == 2
    assert 0 < waits[1] < waits[0] - 0.02


def test_redirect_to_private_target_is_rejected_before_connecting() -> None:
    requests: list[bytes] = []
    with pytest.raises(FeedFetchError, match="forbidden_address"):
        fetch_feed(
            "http://feed.public.example/",
            resolver=_resolver,
            connector=_reply(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\n\r\n",
                requests=requests,
            ),
        )
    assert len(requests) == 1


def test_oversized_headers_are_rejected() -> None:
    response = b"HTTP/1.1 200 OK\r\nX-Large: " + b"a" * 16384 + b"\r\n\r\n"
    with pytest.raises(FeedFetchError, match="invalid_response_headers"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (b"BAD\r\n\r\n", "invalid_response_headers"),
        (b"HTTP/1.1 " + b"2" * 5000 + b"\r\n\r\n", "invalid_response_headers"),
        (b"HTTP/1.1 200 OK\n\n", "invalid_response_headers"),
        (b"HTTP/1.1 200 OK\r\n Folded: no\r\n\r\n", "invalid_response_headers"),
        (b"HTTP/1.1 200 OK\r\nNoColon\r\n\r\n", "invalid_response_headers"),
        (b"HTTP/1.1 200 OK\r\nBad Name: x\r\n\r\n", "invalid_response_headers"),
        (b"HTTP/1.1 200 OK\r\nX-Test: bad\x00value\r\n\r\n", "invalid_response_headers"),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
            "invalid_response_headers",
        ),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
            "invalid_response_headers",
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n", "invalid_response_headers"),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n",
            "invalid_response_headers",
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nab", "invalid_response_body"),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: compress\r\n\r\n",
            "unsupported_transfer_encoding",
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Encoding: br\r\n\r\n", "unsupported_content_encoding"),
        (b"HTTP/1.1 304 Not Modified\r\nContent-Length: 1\r\n\r\nx", "invalid_response_body"),
        (b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n", "http_status"),
        (b"HTTP/1.1 302 Found\r\n\r\n", "invalid_redirect"),
    ],
)
def test_rejects_malformed_or_disallowed_responses(response: bytes, code: str) -> None:
    with pytest.raises(FeedFetchError, match=code):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


@pytest.mark.parametrize(
    ("chunk_body", "expected", "code"),
    [
        (b"4\r\ntest\r\n0\r\n\r\n", b"test", None),
        (b"z\r\n", None, "invalid_response_body"),
        (b"4\r\ntwo\r\n", None, "invalid_response_body"),
        (b"1\r\na\r\n0\r\nX\r\n", None, "invalid_response_body"),
        (b"200001\r\n", None, "response_too_large"),
    ],
)
def test_chunked_framing(chunk_body: bytes, expected: bytes | None, code: str | None) -> None:
    response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk_body
    if code:
        with pytest.raises(FeedFetchError, match=code):
            fetch_feed(
                "http://feed.public.example/", resolver=_resolver, connector=_reply(response)
            )
    else:
        result = fetch_feed(
            "http://feed.public.example/", resolver=_resolver, connector=_reply(response)
        )
        assert result.body == expected


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_valid_compressed_response(encoding: str) -> None:
    compressed = gzip.compress(b"feed") if encoding == "gzip" else zlib.compress(b"feed")
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Encoding: "
        + encoding.encode()
        + b"\r\nContent-Length: "
        + str(len(compressed)).encode()
        + b"\r\n\r\n"
        + compressed
    )
    result = fetch_feed(
        "http://feed.public.example/", resolver=_resolver, connector=_reply(response)
    )
    assert result.body == b"feed"


@pytest.mark.parametrize("compressed", [b"nonsense", gzip.compress(b"feed")[:-2]])
def test_invalid_gzip_response(compressed: bytes) -> None:
    response = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n" + compressed
    with pytest.raises(FeedFetchError, match="invalid_response_body"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


def test_invalid_request_validators_are_omitted() -> None:
    requests: list[bytes] = []
    fetch_feed(
        "http://feed.public.example/",
        etag="bad\r\nInjected: yes",
        last_modified="é",
        resolver=_resolver,
        connector=_reply(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", requests=requests),
    )
    assert b"If-None-Match" not in requests[0]
    assert b"If-Modified-Since" not in requests[0]


def test_network_error_and_deadline_have_stable_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(
        _target: ValidatedTarget,
        _address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        _timeout: float,
    ) -> socket.socket:
        raise OSError("private diagnostic")

    with pytest.raises(FeedFetchError, match="fetch_network"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=broken)
    monkeypatch.setattr(retrieval_http, "REQUEST_TIMEOUT_SECONDS", -1.0)
    with pytest.raises(FeedFetchError, match="fetch_timeout"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=broken)


def test_default_resolver_and_connector_use_pinned_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(
        _hostname: str, _port: int, *, type: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

    monkeypatch.setattr(
        retrieval_http.socket,
        "getaddrinfo",
        fake_getaddrinfo,
    )
    called: list[tuple[str, int]] = []

    class Socket(_FakeSocket):
        def __init__(self) -> None:
            super().__init__(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", "8.8.8.8", None)

        def connect(self, address: tuple[str, int]) -> None:
            called.append(address)

    def make_socket(_family: int, _kind: int) -> socket.socket:
        return Socket()  # type: ignore[return-value]

    monkeypatch.setattr(retrieval_http.socket, "socket", make_socket)
    result = fetch_feed("http://feed.public.example/")
    assert result.status == 200
    assert called == [("8.8.8.8", 80)]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (b"HTTP/1.1 200 OK\r\n\xff: value\r\n\r\n", "invalid_response_headers"),
        (
            b"HTTP/1.1 200 OK\r\n" + b"X-Test: yes\r\n" * 100 + b"\r\n",
            "invalid_response_headers",
        ),
        (b"HTTP/1.1 200 OK\r\n\r\n" + b"a" * (2 * 1024 * 1024 + 1), "response_too_large"),
    ],
)
def test_additional_header_and_unframed_body_bounds(response: bytes, code: str) -> None:
    with pytest.raises(FeedFetchError, match=code):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


def test_chunk_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_http, "MAX_CHUNKS", 1)
    response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\na\r\n0\r\n\r\n"
    with pytest.raises(FeedFetchError, match="response_too_large"):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("http://feed.public.example/", "redirect_loop"),
        ("http://[oops", "invalid_redirect"),
    ],
)
def test_redirect_loop_or_malformed_target(location: str, code: str) -> None:
    response = b"HTTP/1.1 302 Found\r\nLocation: " + location.encode() + b"\r\n\r\n"
    with pytest.raises(FeedFetchError, match=code):
        fetch_feed("http://feed.public.example/", resolver=_resolver, connector=_reply(response))


def test_result_validation_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_result(**_kwargs: object) -> None:
        raise ValueError("private response detail")

    monkeypatch.setattr(retrieval_http, "FeedFetchResult", invalid_result)
    with pytest.raises(FeedFetchError, match="invalid_response"):
        fetch_feed(
            "http://feed.public.example/",
            resolver=_resolver,
            connector=_reply(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
        )
