"""Readiness is a bounded local protocol check before durable work starts."""

import asyncio
import json
import socketserver
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult, FeedPollTarget
from primary_signal.retrieval import client as retrieval_client
from primary_signal.retrieval.api import create_retriever_app
from primary_signal.retrieval.client import MAX_READY_RESPONSE_BYTES, RetrieverClient
from primary_signal.retrieval.protocol import (
    ARTICLE_FETCH_CAPABILITY,
    CAPABILITIES,
    FEED_FETCH_CAPABILITY,
    PROTOCOL_VERSION,
    READY_PATH,
)


def _payload() -> dict[str, object]:
    return {"ready": True, "protocol_version": PROTOCOL_VERSION, "capabilities": list(CAPABILITIES)}


def _response(
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    status: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"} | (headers or {}),
        stream=httpx.ByteStream(json.dumps(_payload()).encode() if body is None else body),
    )


def test_ready_is_static_and_does_not_spend_fetch_rate_budget() -> None:
    calls: list[str] = []

    def fetch(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        calls.append(url)
        return FeedFetchResult(200, url, b"<rss/>")

    app = create_retriever_app(fetch=fetch, max_requests_global=1)

    async def send() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://retriever.example.test"
        ) as client:
            for _ in range(3):
                response = await client.get(READY_PATH)
                assert response.status_code == 200
                assert response.json() == _payload()
                assert response.headers["cache-control"] == "no-store"
            assert not calls
            assert (
                await client.post("/v1/feeds/fetch", json={"url": "https://feed.public.example/"})
            ).status_code == 200
            assert (await client.get(READY_PATH)).status_code == 200
            assert (
                await client.post("/v1/feeds/fetch", json={"url": "https://feed.public.example/"})
            ).status_code == 429

    asyncio.run(send())
    assert calls == ["https://feed.public.example/"]


def test_client_uses_fixed_origin_and_short_readiness_timeout_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    real_async_client = httpx.AsyncClient
    constructor: dict[str, Any] = {}
    ready_constructor: dict[str, Any] = {}
    requests: list[str] = []

    def client_factory(**kwargs: Any) -> httpx.Client:
        constructor.update(kwargs)
        return real_client(**kwargs)

    def ready_client_factory(**kwargs: Any) -> httpx.AsyncClient:
        ready_constructor.update(kwargs)
        return real_async_client(**kwargs)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["accept"] == "application/json"
        if request.method == "GET":
            assert not request.read()
            assert request.extensions["timeout"] == {
                "connect": 2.0,
                "read": 2.0,
                "write": 2.0,
                "pool": 2.0,
            }
            return _response()
        assert request.extensions["timeout"]["read"] == retrieval_client.READ_TIMEOUT_SECONDS
        return _response(
            body=json.dumps(
                {
                    "status": 200,
                    "final_url": "https://feed.public.example/",
                    "body_base64": "PHJzcy8+",
                    "etag": None,
                    "last_modified": None,
                }
            ).encode()
        )

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:8888")
    monkeypatch.setenv("NETRC", "/nonexistent/synthetic-netrc")
    monkeypatch.setattr(retrieval_client.httpx, "Client", client_factory)
    monkeypatch.setattr(retrieval_client.httpx, "AsyncClient", ready_client_factory)
    with RetrieverClient(
        "http://retriever.example.test:8765",
        transport=httpx.MockTransport(respond),
        readiness_transport=httpx.MockTransport(respond),
    ) as client:
        client.require_ready(FEED_FETCH_CAPABILITY)
        assert (
            client.fetch(
                FeedPollTarget(uuid.uuid4(), "https://feed.public.example/", None, None)
            ).body
            == b"<rss/>"
        )
    assert constructor["trust_env"] is False
    assert constructor["follow_redirects"] is False
    assert ready_constructor["trust_env"] is False
    assert ready_constructor["follow_redirects"] is False
    assert requests == [
        "http://retriever.example.test:8765/v1/ready",
        "http://retriever.example.test:8765/v1/feeds/fetch",
    ]


def test_ready_remains_available_when_fetch_admission_is_full() -> None:
    entered, release = threading.Event(), threading.Event()

    def fetch(url: str, _etag: str | None, _modified: str | None) -> FeedFetchResult:
        entered.set()
        release.wait(timeout=5)
        return FeedFetchResult(200, url, b"<rss/>")

    app = create_retriever_app(fetch=fetch, max_concurrent_fetches=1)

    async def send() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://retriever.example.test"
        ) as client:
            running = asyncio.create_task(
                client.post("/v1/feeds/fetch", json={"url": "https://feed.public.example/"})
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                ready = await client.get(READY_PATH)
                assert ready.status_code == 200
                assert ready.json() == _payload()
                busy = await client.post(
                    "/v1/feeds/fetch", json={"url": "https://feed.public.example/"}
                )
                assert busy.status_code == 503
                assert busy.json() == {"error": "retriever_busy"}
            finally:
                release.set()
                assert (await asyncio.wait_for(running, 5)).status_code == 200

    asyncio.run(send())


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_ready_accepts_only_requested_known_capability(capability: str) -> None:
    with RetrieverClient(
        "http://retriever.example.test",
        readiness_transport=httpx.MockTransport(
            lambda _: _response(
                body=json.dumps(_payload() | {"capabilities": [capability]}).encode()
            )
        ),
    ) as client:
        client.require_ready(capability)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"ready": False}, "dependency_unavailable"),
        ({"ready": 1}, "retriever_protocol"),
        ({"ready": "true"}, "retriever_protocol"),
        ({"protocol_version": True}, "retriever_protocol"),
        ({"protocol_version": 2}, "retriever_protocol"),
        ({"protocol_version": "1"}, "retriever_protocol"),
        ({"capabilities": FEED_FETCH_CAPABILITY}, "retriever_protocol"),
        ({"capabilities": None}, "retriever_protocol"),
        ({"capabilities": [FEED_FETCH_CAPABILITY] * 2}, "retriever_protocol"),
        ({"capabilities": [ARTICLE_FETCH_CAPABILITY]}, "retriever_protocol"),
        ({"capabilities": ["unknown.fetch"]}, "retriever_protocol"),
        ({"capabilities": [dict[str, object]()]}, "retriever_protocol"),
        ({"capabilities": []}, "retriever_protocol"),
        ({"extra": "untrusted remote detail"}, "retriever_protocol"),
    ],
)
def test_ready_rejects_incompatible_schema(change: dict[str, object], code: str) -> None:
    with (
        RetrieverClient(
            "http://retriever.example.test",
            readiness_transport=httpx.MockTransport(
                lambda _: _response(body=json.dumps(_payload() | change).encode())
            ),
        ) as client,
        pytest.raises(FeedFetchError, match=f"^{code}$"),
    ):
        client.require_ready(FEED_FETCH_CAPABILITY)


@pytest.mark.parametrize(
    "response",
    [
        _response(body=b'{"ready":true,"ready":false}'),
        _response(body=b"{}"),
        _response(body=b"[]"),
        _response(body=b"not JSON"),
        _response(headers={"content-type": "text/plain"}),
        _response(headers={"content-encoding": "gzip"}),
        _response(headers={"content-length": "invalid"}),
        _response(headers={"content-length": "-1"}),
        _response(headers={"content-length": str(MAX_READY_RESPONSE_BYTES + 1)}),
        _response(body=b"x" * (MAX_READY_RESPONSE_BYTES + 1)),
    ],
)
def test_ready_rejects_malformed_or_unbounded_response(response: httpx.Response) -> None:
    with (
        RetrieverClient(
            "http://retriever.example.test",
            readiness_transport=httpx.MockTransport(lambda _: response),
        ) as client,
        pytest.raises(FeedFetchError, match=r"^retriever_protocol$"),
    ):
        client.require_ready(FEED_FETCH_CAPABILITY)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (204, "dependency_unavailable"),
        (301, "retriever_protocol"),
        (302, "retriever_protocol"),
        (307, "retriever_protocol"),
        (400, "dependency_unavailable"),
        (429, "rate_limited"),
        (500, "dependency_unavailable"),
        (503, "dependency_unavailable"),
    ],
)
def test_ready_never_follows_redirects_or_uses_remote_error_text(status: int, code: str) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return _response(
            status=status,
            headers={"location": "https://other.public.example/"},
            body=b"untrusted remote detail",
        )

    with (
        RetrieverClient(
            "http://retriever.example.test", readiness_transport=httpx.MockTransport(respond)
        ) as client,
        pytest.raises(FeedFetchError, match=f"^{code}$"),
    ):
        client.require_ready(FEED_FETCH_CAPABILITY)
    assert requests == ["http://retriever.example.test/v1/ready"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("untrusted remote detail"), "dependency_timeout"),
        (httpx.ConnectError("untrusted remote detail"), "dependency_unavailable"),
    ],
)
def test_ready_transport_errors_use_safe_codes(error: Exception, code: str) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise error

    with (
        RetrieverClient(
            "http://retriever.example.test", readiness_transport=httpx.MockTransport(respond)
        ) as client,
        pytest.raises(FeedFetchError, match=f"^{code}$"),
    ):
        client.require_ready(FEED_FETCH_CAPABILITY)


def test_ready_bounds_drip_response_by_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_client, "READY_TOTAL_TIMEOUT_SECONDS", 0.05)
    closed: list[bool] = []

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(20):
                await asyncio.sleep(0.02)
                yield b" "
            raise AssertionError("read beyond readiness deadline")

        async def aclose(self) -> None:
            closed.append(True)

    with (
        RetrieverClient(
            "http://retriever.example.test",
            readiness_transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=Drip()
                )
            ),
        ) as client,
        pytest.raises(FeedFetchError, match=r"^dependency_timeout$"),
    ):
        client.require_ready(FEED_FETCH_CAPABILITY)
    assert closed == [True]


@pytest.mark.parametrize("phase", ["headers", "chunk-framing"])
def test_ready_cancels_real_socket_drip_before_idle_timeout(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.setattr(retrieval_client, "READY_TOTAL_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(retrieval_client, "READY_IO_TIMEOUT_SECONDS", 0.5)
    stop, entered, finished = threading.Event(), threading.Event(), threading.Event()

    class Drip(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(1)
            try:
                self.request.recv(4096)
                if phase == "headers":
                    self.request.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Drip: "
                    )
                else:
                    self.request.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n1;extension="
                    )
                entered.set()
                # Every byte arrives inside the idle timeout, but the
                # incomplete header or chunk line would take over a second.
                for _ in range(60):
                    if stop.wait(0.03):
                        break
                    self.request.sendall(b"x")
            except OSError:
                pass  # Cancellation closes the client socket while the peer drips.
            finally:
                finished.set()

    server = socketserver.TCPServer(("127.0.0.1", 0), Drip)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        with RetrieverClient(f"http://127.0.0.1:{server.server_address[1]}") as client:
            started = time.monotonic()
            with pytest.raises(FeedFetchError, match=r"^dependency_timeout$"):
                client.require_ready(FEED_FETCH_CAPABILITY)
            elapsed = time.monotonic() - started
        assert entered.is_set(), "probe did not reach the dripped response"
        assert elapsed < 1.2, "probe used the idle timeout instead of the overall deadline"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert finished.is_set()
    assert not server_thread.is_alive()


def test_unknown_required_capability_fails_before_request() -> None:
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unknown local capability reached transport")

    with (
        RetrieverClient(
            "http://retriever.example.test", readiness_transport=httpx.MockTransport(forbidden)
        ) as client,
        pytest.raises(ValueError, match="unknown retriever capability"),
    ):
        client.require_ready("unknown.fetch")
