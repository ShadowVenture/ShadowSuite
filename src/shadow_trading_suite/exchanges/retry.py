"""Bounded, deterministic retry policy (exponential backoff, Retry-After aware)."""

from dataclasses import dataclass, field

DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    max_retry_after_s: float = 30.0
    retry_statuses: frozenset[int] = field(default=DEFAULT_RETRY_STATUSES)

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if self.base_delay_s < 0 or self.max_delay_s < self.base_delay_s:
            raise ValueError("invalid backoff delays")

    def backoff(self, attempt: int) -> float:
        """Delay after the given 1-based failed attempt."""
        return float(min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1)))

    def delay_for(self, attempt: int, retry_after: str | None) -> float | None:
        """Delay before retrying, or None if the server asks us to wait too long."""
        if retry_after is not None and retry_after.strip().isdigit():
            seconds = float(retry_after.strip())
            return None if seconds > self.max_retry_after_s else seconds
        return self.backoff(attempt)
