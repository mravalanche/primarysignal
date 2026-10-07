"""Finite-wait DNS adapter for the feed retrieval policy.

The platform lookup cannot be cancelled once started. A timed-out caller
discards its private result queue; the daemon worker keeps a capacity slot
until the underlying lookup actually exits.
"""

import math
import queue
import socket
import threading
from collections.abc import Callable, Sequence

DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_IN_FLIGHT = 4

type Lookup = Callable[[str, int], Sequence[str]]


class DnsTimeoutError(TimeoutError):
    """The system lookup did not return before the caller deadline."""


class DnsCapacityError(OSError):
    """All bounded system lookup slots are occupied."""


class DnsLookupError(OSError):
    """The system lookup failed without exposing resolver diagnostics."""


def _system_lookup(hostname: str, port: int) -> Sequence[str]:
    return tuple(
        str(row[4][0]) for row in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    )


class BoundedDnsResolver:
    """Resolve in bounded daemon workers, returning only on-time answers."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        lookup: Lookup = _system_lookup,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("DNS timeout must be finite and positive")
        if type(max_in_flight) is not int or max_in_flight <= 0:
            raise ValueError("DNS capacity must be a positive integer")
        self._timeout_seconds = timeout_seconds
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._lookup = lookup

    def __call__(self, hostname: str, port: int) -> Sequence[str]:
        return self.resolve(hostname, port)

    def resolve(
        self, hostname: str, port: int, *, timeout_seconds: float | None = None
    ) -> Sequence[str]:
        """Wait at most the configured bound and the caller's remaining time."""

        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise DnsTimeoutError("dns_timeout")
        timeout = (
            self._timeout_seconds
            if timeout_seconds is None
            else min(self._timeout_seconds, timeout_seconds)
        )
        if not self._slots.acquire(blocking=False):
            raise DnsCapacityError("dns_capacity")

        result_queue: queue.Queue[tuple[Sequence[str] | None, bool]] = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                result_queue.put((tuple(self._lookup(hostname, port)), False))
            except Exception:
                result_queue.put((None, True))
            finally:
                self._slots.release()

        worker = threading.Thread(target=work, name="feed-dns-lookup", daemon=True)
        try:
            worker.start()
        except RuntimeError as error:
            self._slots.release()
            raise DnsCapacityError("dns_capacity") from error

        try:
            addresses, failed = result_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise DnsTimeoutError("dns_timeout") from error
        if failed or addresses is None:
            raise DnsLookupError("dns_lookup")
        return addresses


DEFAULT_RESOLVER = BoundedDnsResolver()
