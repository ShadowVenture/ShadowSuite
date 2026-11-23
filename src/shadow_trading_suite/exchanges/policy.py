"""Per-venue allowlist of public GET endpoints and query parameters."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.exchanges.errors import TransportPolicyError

_PATH_CHARS = re.compile(r"/[A-Za-z0-9_\-/]+")
_PARAM_VALUE = re.compile(r"[A-Za-z0-9_.:@\-]{1,64}")

ParamValue = str | int


@dataclass(frozen=True)
class EndpointPolicy:
    venue: Venue
    base_url: str
    path_patterns: tuple[str, ...]
    allowed_params: frozenset[str]
    _compiled: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise TransportPolicyError(f"base_url must be a bare https origin: {self.base_url!r}")
        object.__setattr__(self, "_compiled", tuple(re.compile(p) for p in self.path_patterns))

    def build_url(self, path: str, params: Mapping[str, ParamValue] | None = None) -> str:
        if not _PATH_CHARS.fullmatch(path) or not any(p.fullmatch(path) for p in self._compiled):
            raise TransportPolicyError(f"{self.venue.value}: path {path!r} is not allowlisted")
        query: list[tuple[str, str]] = []
        for name, value in sorted((params or {}).items()):
            if name not in self.allowed_params:
                raise TransportPolicyError(
                    f"{self.venue.value}: query parameter {name!r} is not allowlisted"
                )
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise TransportPolicyError(f"query parameter {name!r} has an invalid value type")
            text = str(value)
            if not _PARAM_VALUE.fullmatch(text):
                raise TransportPolicyError(f"query parameter {name!r} has an unsafe value")
            query.append((name, text))
        url = self.base_url.rstrip("/") + path
        return f"{url}?{urlencode(query)}" if query else url
