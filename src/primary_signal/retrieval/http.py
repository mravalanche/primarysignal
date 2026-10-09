"""Bounded, address-pinned HTTP retrieval for feed documents."""

import hashlib
import ipaddress
import socket
import ssl
import time
import zlib
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult
from primary_signal.retrieval.article import (
    MAX_ARTICLE_URL_CHARS,
    ArticleFetchResult,
    RedirectHop,
)
from primary_signal.retrieval.dns import DEFAULT_RESOLVER
from primary_signal.retrieval.extract import InvalidArticle, extract_article_html
from primary_signal.retrieval.policy import PolicyError, ValidatedTarget, validate_target

MAX_REDIRECTS = 5
MAX_HEADER_BYTES = 16 * 1024
MAX_HEADER_LINES = 100
MAX_CHUNKS = 4096
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
SOCKET_TIMEOUT_SECONDS = 5.0
READ_SIZE = 8192

type Resolver = Callable[[str, int], Sequence[str]]
type Connector = Callable[
    [ValidatedTarget, ipaddress.IPv4Address | ipaddress.IPv6Address, float], socket.socket
]


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes


class _DeadlineStream:
    def __init__(self, stream: BinaryIO, connection: socket.socket, deadline: float) -> None:
        self._stream = stream
        self._connection = connection
        self._deadline = deadline

    def read(self, size: int) -> bytes:
        self._connection.settimeout(_remaining(self._deadline))
        return self._stream.read(size)

    def readline(self, size: int) -> bytes:
        self._connection.settimeout(_remaining(self._deadline))
        return self._stream.readline(size)


def _resolve(hostname: str, port: int) -> Sequence[str]:
    return DEFAULT_RESOLVER(hostname, port)


def _connect(
    target: ValidatedTarget,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    timeout: float,
) -> socket.socket:
    family = socket.AF_INET if isinstance(address, ipaddress.IPv4Address) else socket.AF_INET6
    connection = socket.socket(family, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        if family == socket.AF_INET:
            connection.connect((str(address), target.port))
        else:
            connection.connect((str(address), target.port, 0, 0))
        return connection
    except BaseException:
        connection.close()
        raise


def _safe_validator(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value
        or len(value) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return None
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FeedFetchError("fetch_timeout")
    return min(SOCKET_TIMEOUT_SECONDS, remaining)


def _readline(stream: _DeadlineStream, budget: list[int], deadline: float) -> bytes:
    _remaining(deadline)
    line = stream.readline(min(budget[0] + 1, MAX_HEADER_BYTES + 1))
    budget[0] -= len(line)
    if budget[0] < 0 or not line.endswith(b"\r\n"):
        raise FeedFetchError("invalid_response_headers")
    return line[:-2]


def _headers(stream: _DeadlineStream, deadline: float) -> tuple[int, dict[str, str]]:
    budget = [MAX_HEADER_BYTES]
    status_line = _readline(stream, budget, deadline)
    parts = status_line.split(b" ", 2)
    if (
        len(parts) < 2
        or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1")
        or len(parts[1]) != 3
        or not parts[1].isascii()
        or not parts[1].isdigit()
    ):
        raise FeedFetchError("invalid_response_headers")
    status = int(parts[1])
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADER_LINES):
        line = _readline(stream, budget, deadline)
        if not line:
            break
        if line[:1] in (b" ", b"\t") or b":" not in line:
            raise FeedFetchError("invalid_response_headers")
        name, value = line.split(b":", 1)
        try:
            key = name.decode("ascii").lower()
        except UnicodeDecodeError as error:
            raise FeedFetchError("invalid_response_headers") from error
        if not key or any(
            character not in "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyz"
            for character in key
        ):
            raise FeedFetchError("invalid_response_headers")
        content = value.strip().decode("iso-8859-1")
        if any(
            (ord(character) < 32 and character != "\t") or ord(character) == 127
            for character in content
        ):
            raise FeedFetchError("invalid_response_headers")
        if key in headers and key in ("content-length", "transfer-encoding", "location"):
            raise FeedFetchError("invalid_response_headers")
        headers[key] = content
    else:
        raise FeedFetchError("invalid_response_headers")
    if "transfer-encoding" in headers and "content-length" in headers:
        raise FeedFetchError("invalid_response_headers")
    return status, headers


def _chunks(stream: _DeadlineStream, headers: dict[str, str], deadline: float) -> Sequence[bytes]:
    encoding = headers.get("transfer-encoding", "").lower()
    if encoding:
        if encoding != "chunked":
            raise FeedFetchError("unsupported_transfer_encoding")
        chunks: list[bytes] = []
        consumed = 0
        for _ in range(MAX_CHUNKS):
            line = _readline(stream, [64], deadline)
            size_text = line.split(b";", 1)[0]
            try:
                size = int(size_text, 16)
            except ValueError as error:
                raise FeedFetchError("invalid_response_body") from error
            if size < 0 or size > MAX_COMPRESSED_BYTES - consumed:
                raise FeedFetchError("response_too_large")
            if size == 0:
                if stream.read(2) != b"\r\n":
                    raise FeedFetchError("invalid_response_body")
                return chunks
            data = stream.read(size)
            if len(data) != size or stream.read(2) != b"\r\n":
                raise FeedFetchError("invalid_response_body")
            chunks.append(data)
            consumed += size
        raise FeedFetchError("response_too_large")
    length = headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise FeedFetchError("invalid_response_headers")
        remaining = int(length)
        if remaining > MAX_COMPRESSED_BYTES:
            raise FeedFetchError("response_too_large")
    else:
        remaining = MAX_COMPRESSED_BYTES + 1
    chunks = []
    while remaining:
        _remaining(deadline)
        data = stream.read(min(READ_SIZE, remaining))
        if not data:
            if length is not None:
                raise FeedFetchError("invalid_response_body")
            break
        chunks.append(data)
        remaining -= len(data)
        if length is None and remaining == 0:
            raise FeedFetchError("response_too_large")
    return chunks


def _body(stream: _DeadlineStream, headers: dict[str, str], deadline: float) -> bytes:
    encoding = headers.get("content-encoding", "identity").lower()
    if encoding not in ("identity", "gzip", "deflate"):
        raise FeedFetchError("unsupported_content_encoding")
    decoder = (
        None
        if encoding == "identity"
        else zlib.decompressobj(zlib.MAX_WBITS | 16 if encoding == "gzip" else zlib.MAX_WBITS)
    )
    output = bytearray()
    for chunk in _chunks(stream, headers, deadline):
        try:
            decoded = (
                chunk
                if decoder is None
                else decoder.decompress(chunk, MAX_BODY_BYTES + 1 - len(output))
            )
        except zlib.error as error:
            raise FeedFetchError("invalid_response_body") from error
        output.extend(decoded)
        if len(output) > MAX_BODY_BYTES or (decoder is not None and decoder.unconsumed_tail):
            raise FeedFetchError("response_too_large")
    if decoder is not None and (not decoder.eof or decoder.unused_data):
        raise FeedFetchError("invalid_response_body")
    return bytes(output)


def _request(
    target: ValidatedTarget,
    *,
    etag: str | None,
    last_modified: str | None,
    deadline: float,
    connector: Connector,
    accept: str = "application/rss+xml, application/atom+xml, application/xml, text/xml",
    user_agent: str = "PrimarySignalFeedPoll/1",
) -> _Response:
    address = target.addresses[0]
    raw = connector(target, address, _remaining(deadline))
    with closing(raw):
        if ipaddress.ip_address(raw.getpeername()[0]) != address:
            raise FeedFetchError("peer_mismatch")
        connection = raw
        if target.scheme == "https":
            context = ssl.create_default_context()
            connection = context.wrap_socket(raw, server_hostname=target.hostname)
        with closing(connection):
            connection.settimeout(_remaining(deadline))
            host = target.hostname
            if ":" in host:
                host = f"[{host}]"
            request_lines = [
                f"GET {target.path_and_query} HTTP/1.1",
                f"Host: {host}",
                f"User-Agent: {user_agent}",
                f"Accept: {accept}",
                "Accept-Encoding: gzip, deflate",
                "Connection: close",
            ]
            if etag:
                request_lines.append(f"If-None-Match: {etag}")
            if last_modified:
                request_lines.append(f"If-Modified-Since: {last_modified}")
            connection.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
            with connection.makefile("rb") as raw_stream:
                stream = _DeadlineStream(raw_stream, connection, deadline)
                status, headers = _headers(stream, deadline)
                if status == 304 and (
                    headers.get("content-length", "0") != "0" or "transfer-encoding" in headers
                ):
                    raise FeedFetchError("invalid_response_body")
                body = (
                    b""
                    if status in (301, 302, 303, 307, 308, 304)
                    else _body(stream, headers, deadline)
                )
                return _Response(status, headers, body)


def fetch_feed(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    resolver: Resolver = _resolve,
    connector: Connector = _connect,
) -> FeedFetchResult:
    """Fetch a feed through a validated address, rechecking each redirect.

    The request deadline covers socket work and is checked after DNS lookup.
    The default resolver caps caller wait and concurrent system lookups. A
    timed-out platform ``getaddrinfo`` call continues in a bounded daemon
    worker until it exits.
    """

    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    current = url
    validators = (_safe_validator(etag), _safe_validator(last_modified))
    visited: set[str] = set()

    def bounded_resolve(hostname: str, port: int) -> Sequence[str]:
        return DEFAULT_RESOLVER.resolve(hostname, port, timeout_seconds=_remaining(deadline))

    active_resolver: Resolver = bounded_resolve if resolver is _resolve else resolver
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            if current in visited:
                raise FeedFetchError("redirect_loop")
            visited.add(current)
            target = validate_target(current, active_resolver)
            _remaining(deadline)
            response = _request(
                target,
                etag=validators[0],
                last_modified=validators[1],
                deadline=deadline,
                connector=connector,
            )
            _remaining(deadline)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise FeedFetchError("invalid_redirect")
                try:
                    following = urljoin(current, location)
                    following_parts = urlsplit(following)
                    current_parts = urlsplit(current)
                except ValueError as error:
                    raise FeedFetchError("invalid_redirect") from error
                if target.scheme == "https" and following_parts.scheme.lower() != "https":
                    raise FeedFetchError("https_downgrade")
                if (
                    following_parts.scheme.lower() != target.scheme
                    or following_parts.netloc.lower() != current_parts.netloc.lower()
                ):
                    validators = (None, None)
                current = following
                continue
            if response.status not in (200, 304):
                raise FeedFetchError("http_status")
            try:
                return FeedFetchResult(
                    status=response.status,
                    final_url=current,
                    body=response.body,
                    etag=_safe_validator(response.headers.get("etag")),
                    last_modified=_safe_validator(response.headers.get("last-modified")),
                )
            except ValueError as error:
                raise FeedFetchError("invalid_response") from error
    except PolicyError as error:
        raise FeedFetchError(error.code) from error
    except (OSError, ssl.SSLError) as error:
        raise FeedFetchError("fetch_network") from error
    raise FeedFetchError("invalid_redirect")


def fetch_article(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    resolver: Resolver = _resolve,
    connector: Connector = _connect,
) -> ArticleFetchResult:
    """Fetch and extract one article inside the address-pinned retriever."""

    if len(url) > MAX_ARTICLE_URL_CHARS:
        raise FeedFetchError("url")
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    current = url
    validators = (_safe_validator(etag), _safe_validator(last_modified))
    visited: set[str] = set()
    redirects: list[RedirectHop] = []
    pending_redirect: tuple[int, str] | None = None

    def bounded_resolve(hostname: str, port: int) -> Sequence[str]:
        return DEFAULT_RESOLVER.resolve(hostname, port, timeout_seconds=_remaining(deadline))

    active_resolver: Resolver = bounded_resolve if resolver is _resolve else resolver
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            if current in visited:
                raise FeedFetchError("redirect_loop")
            visited.add(current)
            target = validate_target(current, active_resolver)
            _remaining(deadline)
            if pending_redirect is not None:
                try:
                    redirects.append(RedirectHop(*pending_redirect))
                except ValueError as error:
                    raise FeedFetchError("invalid_redirect") from error
                pending_redirect = None
            response = _request(
                target,
                etag=validators[0],
                last_modified=validators[1],
                deadline=deadline,
                connector=connector,
                accept="text/html, application/xhtml+xml",
                user_agent="PrimarySignalArticleFetch/1",
            )
            _remaining(deadline)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise FeedFetchError("invalid_redirect")
                try:
                    following = urljoin(current, location)
                    following_parts = urlsplit(following)
                    current_parts = urlsplit(current)
                except ValueError as error:
                    raise FeedFetchError("invalid_redirect") from error
                if len(following) > MAX_ARTICLE_URL_CHARS:
                    raise FeedFetchError("invalid_redirect")
                if target.scheme == "https" and following_parts.scheme.lower() != "https":
                    raise FeedFetchError("https_downgrade")
                if (
                    following_parts.scheme.lower() != target.scheme
                    or following_parts.netloc.lower() != current_parts.netloc.lower()
                ):
                    validators = (None, None)
                current = following
                pending_redirect = (response.status, following)
                continue
            if response.status not in (200, 304):
                raise FeedFetchError("http_status")
            content_type = response.headers.get("content-type")
            if content_type is not None and len(content_type) > 256:
                raise FeedFetchError("invalid_response_headers")
            try:
                extraction = (
                    extract_article_html(response.body, content_type)
                    if response.status == 200
                    else None
                )
                return ArticleFetchResult(
                    status=response.status,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    content_type=content_type,
                    decoded_byte_count=len(response.body),
                    raw_response_hash=(
                        hashlib.sha256(response.body).hexdigest()
                        if response.status == 200
                        else None
                    ),
                    extraction=extraction,
                    etag=_safe_validator(response.headers.get("etag")),
                    last_modified=_safe_validator(response.headers.get("last-modified")),
                )
            except InvalidArticle as error:
                raise FeedFetchError(error.code) from error
            except ValueError as error:
                raise FeedFetchError("invalid_response") from error
    except PolicyError as error:
        raise FeedFetchError(error.code) from error
    except (OSError, ssl.SSLError) as error:
        raise FeedFetchError("fetch_network") from error
    raise FeedFetchError("invalid_redirect")
