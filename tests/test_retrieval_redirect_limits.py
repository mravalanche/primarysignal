"""Redirect admissions use the validated destination, before a socket opens."""

import asyncio
import io
import ipaddress
import socket
import threading
from collections.abc import Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from primary_signal.retrieval import api, http
from primary_signal.retrieval.policy import ValidatedTarget

START = "http://first.public.example/feed"
OTHER = "http://second.public.example/feed"
DEST = "http://destination.public.example/story"


class _Socket:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def getpeername(self) -> tuple[str, int]:
        return "8.8.8.8", 80

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, _data: bytes) -> None:
        pass

    def makefile(self, _mode: str) -> Any:
        return io.BytesIO(self.body)

    def close(self) -> None:
        pass


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    connector: Callable[
        [ValidatedTarget, ipaddress.IPv4Address | ipaddress.IPv6Address, float], socket.socket
    ],
) -> None:
    original_request = http._request  # pyright: ignore[reportPrivateUsage]

    class Resolver:
        def resolve(self, _hostname: str, _port: int, *, timeout_seconds: float) -> tuple[str, ...]:
            assert timeout_seconds > 0
            return ("8.8.8.8",)

    connector_impl = connector

    def request(
        target: ValidatedTarget,
        *,
        etag: str | None,
        last_modified: str | None,
        deadline: float,
        connector: Any,
        accept: str = "application/rss+xml, application/atom+xml, application/xml, text/xml",
        user_agent: str = "PrimarySignalFeedPoll/1",
    ) -> Any:
        return original_request(
            target,
            etag=etag,
            last_modified=last_modified,
            deadline=deadline,
            connector=connector_impl,
            accept=accept,
            user_agent=user_agent,
        )

    monkeypatch.setattr(http, "DEFAULT_RESOLVER", Resolver())
    monkeypatch.setattr(http, "_request", request)


def _send(app: Any, path: str, urls: tuple[str, ...]) -> list[Response]:
    async def send() -> list[Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            return [await client.post(path, json={"url": url}) for url in urls]

    return asyncio.run(send())


@pytest.mark.parametrize("path", ["/v1/feeds/fetch", "/v1/articles/fetch"])
def test_redirect_destination_is_rate_limited_without_connecting(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    connected: list[str] = []

    def connector(target: ValidatedTarget, _address: Any, _timeout: float) -> socket.socket:
        connected.append(target.hostname)
        if target.hostname != "destination.public.example":
            return _Socket(f"HTTP/1.1 302 Found\r\nLocation: {DEST}\r\n\r\n".encode())  # type: ignore[return-value]
        if path.endswith("articles/fetch"):
            body = b"<html><head><title>Example article</title></head><body>Example text.</body></html>"
            headers = (
                f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\n\r\n"
            )
            return _Socket(headers.encode() + body)  # type: ignore[return-value]
        return _Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")  # type: ignore[return-value]

    _install_transport(monkeypatch, connector)
    app = api.create_retriever_app(max_requests_per_origin=1, max_requests_global=2)
    first, denied = _send(app, path, (START, OTHER))
    assert first.status_code == 200
    assert denied.status_code == 429
    assert denied.json() == {"error": "origin_rate_limited"}
    assert connected == [
        "first.public.example",
        "destination.public.example",
        "second.public.example",
    ]


def test_same_host_redirect_spends_another_origin_token_and_no_global_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[str] = []

    def connector(target: ValidatedTarget, _address: Any, _timeout: float) -> socket.socket:
        connected.append(target.hostname)
        return _Socket(b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n")  # type: ignore[return-value]

    _install_transport(monkeypatch, connector)
    app = api.create_retriever_app(
        max_requests_per_origin=1, max_concurrent_per_origin=1, max_requests_global=2
    )
    denied, available, global_denied = _send(app, "/v1/feeds/fetch", (START, OTHER, OTHER))
    assert denied.status_code == 429
    assert denied.json() == {"error": "origin_rate_limited"}
    assert available.status_code == 429  # Its own same-host redirect also spends a token.
    assert global_denied.status_code == 429
    assert global_denied.json() == {"error": "global_rate_limited"}
    assert connected == ["first.public.example", "second.public.example"]


def test_private_redirect_is_rejected_before_admission_or_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[str] = []
    admitted: list[str] = []

    def connector(target: ValidatedTarget, _address: Any, _timeout: float) -> socket.socket:
        connected.append(target.hostname)
        return _Socket(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\n\r\n")  # type: ignore[return-value]

    original = api._RequestLimiter.redirect_lease  # pyright: ignore[reportPrivateUsage]

    def observe(self: Any, hostname: str, *, initial_hostname: str) -> Any:
        admitted.append(hostname)
        return original(self, hostname, initial_hostname=initial_hostname)

    monkeypatch.setattr(api._RequestLimiter, "redirect_lease", observe)  # pyright: ignore[reportPrivateUsage]
    _install_transport(monkeypatch, connector)
    response = _send(api.create_retriever_app(), "/v1/feeds/fetch", (START,))[0]
    assert response.status_code == 502
    assert response.json() == {"error": "forbidden_address"}
    assert connected == ["first.public.example"]
    assert admitted == []


def test_cross_host_redirect_holds_destination_capacity_only_during_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    connected: list[str] = []

    def connector(target: ValidatedTarget, _address: Any, _timeout: float) -> socket.socket:
        connected.append(target.hostname)
        if target.hostname != "destination.public.example":
            return _Socket(f"HTTP/1.1 302 Found\r\nLocation: {DEST}\r\n\r\n".encode())  # type: ignore[return-value]
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return _Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")  # type: ignore[return-value]

    _install_transport(monkeypatch, connector)
    app = api.create_retriever_app(max_concurrent_per_origin=1, max_requests_per_origin=3)

    async def send() -> tuple[Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://retriever"
        ) as client:
            first_task = asyncio.create_task(client.post("/v1/feeds/fetch", json={"url": START}))
            assert await asyncio.to_thread(entered.wait, 3)
            second = await client.post("/v1/feeds/fetch", json={"url": OTHER})
            release.set()
            return await first_task, second

    try:
        first, denied = asyncio.run(send())
    finally:
        release.set()
    assert first.status_code == 200
    assert denied.status_code == 503
    assert denied.json() == {"error": "retriever_busy"}
    assert connected.count("destination.public.example") == 1


def test_failed_destination_request_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def connector(target: ValidatedTarget, _address: Any, _timeout: float) -> socket.socket:
        nonlocal attempts
        if target.hostname != "destination.public.example":
            return _Socket(f"HTTP/1.1 302 Found\r\nLocation: {DEST}\r\n\r\n".encode())  # type: ignore[return-value]
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic network failure")
        return _Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")  # type: ignore[return-value]

    _install_transport(monkeypatch, connector)
    app = api.create_retriever_app(max_concurrent_per_origin=1, max_requests_per_origin=2)
    failed, recovered = _send(app, "/v1/feeds/fetch", (START, OTHER))
    assert failed.status_code == 502
    assert failed.json() == {"error": "fetch_network"}
    assert recovered.status_code == 200
    assert attempts == 2
