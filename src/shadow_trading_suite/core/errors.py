"""Project-wide exception types."""


class SafetyViolationError(Exception):
    """A paper-trading-only invariant was violated. Never catch-and-continue."""


class ConfigError(Exception):
    """Configuration is invalid or ambiguous. The process must not start."""


class DataQualityError(Exception):
    """Requested market data is missing, stale, inconsistent or rejected. Do not trade on it."""
