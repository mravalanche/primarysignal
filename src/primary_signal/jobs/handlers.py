"""Explicit process-local bindings for durable job handlers."""

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from primary_signal.jobs.catalogue import JobCatalogue
from primary_signal.jobs.contracts import JobPayload


class JobHandler[PayloadT: JobPayload](Protocol):
    """A handler explicitly wired into a processor at startup."""

    def __call__(self, payload: PayloadT) -> None: ...


@dataclass(frozen=True, slots=True)
class JobHandlerBinding[PayloadT: JobPayload]:
    """A local callable bound to one versioned catalogue entry."""

    job_type: str
    payload_version: int
    handler: JobHandler[PayloadT]


class JobHandlers:
    """Immutable handler bindings validated against a contract catalogue."""

    def __init__(
        self,
        catalogue: JobCatalogue,
        bindings: Iterable[JobHandlerBinding[Any]],
    ) -> None:
        entries: dict[tuple[str, int], JobHandlerBinding[Any]] = {}
        for binding in bindings:
            key = (binding.job_type, binding.payload_version)
            catalogue.get(*key)
            if key in entries:
                raise ValueError(
                    f"duplicate job handler: {binding.job_type} v{binding.payload_version}"
                )
            entries[key] = binding
        self._catalogue = catalogue
        self._entries = MappingProxyType(entries)

    def get(self, job_type: str, payload_version: int) -> JobHandler[Any]:
        """Return an explicitly bound handler for a known contract."""

        self._catalogue.get(job_type, payload_version)
        try:
            return self._entries[(job_type, payload_version)].handler
        except KeyError as error:
            raise UnknownJobHandler(
                f"no handler bound for job contract: {job_type} v{payload_version}"
            ) from error

    def require_complete_queue(self, queue: str) -> None:
        """Reject startup when any contract in a consumed queue is unbound."""

        missing = [
            f"{contract.job_type} v{contract.payload_version}"
            for contract in self._catalogue.contracts_for_queue(queue)
            if (contract.job_type, contract.payload_version) not in self._entries
        ]
        if missing:
            raise UnknownJobHandler(f"queue {queue!r} has no handler for: {', '.join(missing)}")


class UnknownJobHandler(ValueError):
    """Raised when a known job contract has no local handler binding."""
