"""Synthetic tests for bounded system DNS lookup workers."""

import socket
import threading
import time
from collections.abc import Sequence

import pytest

from primary_signal.retrieval import dns


def test_resolver_returns_addresses_without_live_dns() -> None:
    calls: list[tuple[str, int]] = []

    def lookup(hostname: str, port: int) -> Sequence[str]:
        calls.append((hostname, port))
        return ("8.8.8.8", "1.1.1.1")

    resolver = dns.BoundedDnsResolver(lookup=lookup)
    assert resolver("feed.public.example", 443) == ("8.8.8.8", "1.1.1.1")
    assert calls == [("feed.public.example", 443)]


def test_timeout_preserves_capacity_until_worker_exits_and_discards_late_result() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def lookup(hostname: str, _port: int) -> Sequence[str]:
        if hostname == "slow.public.example":
            started.set()
            release.wait(timeout=2)
            finished.set()
            return ("8.8.8.8",)
        return ("1.1.1.1",)

    resolver = dns.BoundedDnsResolver(timeout_seconds=0.02, max_in_flight=1, lookup=lookup)
    began = time.monotonic()
    try:
        with pytest.raises(dns.DnsTimeoutError, match="dns_timeout"):
            resolver("slow.public.example", 80)
        assert started.is_set()
        assert time.monotonic() - began < 0.5
        with pytest.raises(dns.DnsCapacityError, match="dns_capacity"):
            resolver("next.public.example", 80)
    finally:
        release.set()
    assert finished.wait(timeout=1)
    deadline = time.monotonic() + 1
    while True:
        try:
            answer = resolver("next.public.example", 80)
            break
        except dns.DnsCapacityError:
            assert time.monotonic() < deadline
            time.sleep(0.005)
    assert answer == ("1.1.1.1",)


def test_caller_deadline_clips_configured_dns_wait() -> None:
    release = threading.Event()

    def lookup(_hostname: str, _port: int) -> Sequence[str]:
        release.wait(timeout=1)
        return ("8.8.8.8",)

    resolver = dns.BoundedDnsResolver(timeout_seconds=2.0, max_in_flight=1, lookup=lookup)
    began = time.monotonic()
    try:
        with pytest.raises(dns.DnsTimeoutError, match="dns_timeout"):
            resolver.resolve("slow.public.example", 80, timeout_seconds=0.02)
        assert time.monotonic() - began < 0.5
    finally:
        release.set()


def test_lookup_failure_has_stable_error_without_diagnostics() -> None:
    def lookup(_hostname: str, _port: int) -> Sequence[str]:
        raise OSError("private resolver detail")

    resolver = dns.BoundedDnsResolver(lookup=lookup)
    with pytest.raises(dns.DnsLookupError, match="dns_lookup") as caught:
        resolver("feed.public.example", 80)
    assert "private resolver detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("timeout", "capacity"),
    [(0.0, 1), (float("inf"), 1), (float("nan"), 1), (1.0, 0), (1.0, True)],
)
def test_rejects_unbounded_configuration(timeout: float, capacity: int) -> None:
    with pytest.raises(ValueError):
        dns.BoundedDnsResolver(timeout_seconds=timeout, max_in_flight=capacity)


def test_system_lookup_requests_stream_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(
        hostname: str, port: int, *, type: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert (hostname, port, type) == ("feed.public.example", 443, socket.SOCK_STREAM)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(dns.socket, "getaddrinfo", getaddrinfo)
    assert dns.DEFAULT_RESOLVER("feed.public.example", 443) == ("8.8.8.8",)
