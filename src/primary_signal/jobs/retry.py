"""Bounded retry policy with injectable jitter."""

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay_seconds: float = 5.0
    maximum_delay_seconds: float = 900.0
    retryable_error_codes: frozenset[str] = field(default_factory=lambda: frozenset[str]())

    def __post_init__(self) -> None:
        if not isfinite(self.base_delay_seconds) or self.base_delay_seconds <= 0:
            raise ValueError("base retry delay must be positive")
        if (
            not isfinite(self.maximum_delay_seconds)
            or self.maximum_delay_seconds < self.base_delay_seconds
            or self.maximum_delay_seconds > 86_400
        ):
            raise ValueError("maximum retry delay must be finite, bounded, and not below the base")

    def permits(self, error_code: str) -> bool:
        return error_code in self.retryable_error_codes

    def delay_seconds(self, attempt_number: int, *, random_value: Callable[[], float]) -> float:
        """Return capped exponential delay; random_value must be in [0, 1]."""

        if attempt_number < 1:
            raise ValueError("attempt number must be positive")
        sample = random_value()
        if not 0 <= sample <= 1:
            raise ValueError("random value must be between zero and one")
        uncapped = self.base_delay_seconds * (2 ** (attempt_number - 1))
        cap = min(uncapped, self.maximum_delay_seconds)
        return (cap / 2) + (sample * cap / 2)
