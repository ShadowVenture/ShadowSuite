"""Exchange access errors. Any of these means: no fresh data — callers must fail closed."""

from shadow_trading_suite.core.errors import SafetyViolationError


class ExchangeError(Exception):
    """Base class for public market-data access failures."""


class TransportPolicyError(SafetyViolationError):
    """A request violated the read-only public transport policy. Never retried."""


class TransportError(ExchangeError):
    """Network-level failure (DNS, connect, TLS, timeout). Retryable."""


class ExchangeHttpError(ExchangeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class RateLimitedError(ExchangeHttpError):
    """HTTP 429 that could not be absorbed by the retry policy."""


class ExchangeUnavailableError(ExchangeError):
    """Retries exhausted; the venue is treated as unavailable."""


class ResponseFormatError(ExchangeError):
    """The response was not in the expected shape. Data is discarded."""


class VenueApiError(ExchangeError):
    """The venue returned an application-level error envelope."""
