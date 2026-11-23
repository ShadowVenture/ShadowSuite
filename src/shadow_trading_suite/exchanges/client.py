"""Read-only public REST client: allowlisted GET, rate-limited, bounded retries."""

import json
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from shadow_trading_suite import __version__
from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.exchanges.errors import (
    ExchangeError,
    ExchangeHttpError,
    ExchangeUnavailableError,
    RateLimitedError,
    ResponseFormatError,
    TransportError,
)
from shadow_trading_suite.exchanges.http import Fetcher, HttpRequest, HttpResponse
from shadow_trading_suite.exchanges.policy import EndpointPolicy, ParamValue
from shadow_trading_suite.exchanges.ratelimit import RateLimiter
from shadow_trading_suite.exchanges.retry import RetryPolicy

USER_AGENT = f"shadow-trading-suite/{__version__} (paper-only; public market data)"
_MAX_ERROR_SNIPPET = 200


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} in response")


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"), parse_float=Decimal, parse_constant=_reject_constant
        )
    except ValueError as exc:
        raise ResponseFormatError(f"invalid JSON response: {exc}") from exc


def _snippet(resp: HttpResponse) -> str:
    return resp.body[:_MAX_ERROR_SNIPPET].decode("utf-8", errors="replace")


class PublicRestClient:
    def __init__(
        self,
        policy: EndpointPolicy,
        fetcher: Fetcher,
        limiter: RateLimiter,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 10.0,
    ) -> None:
        self._policy = policy
        self._fetcher = fetcher
        self._limiter = limiter
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._timeout_s = timeout_s

    @property
    def venue(self) -> Venue:
        return self._policy.venue

    def get_json(self, path: str, params: Mapping[str, ParamValue] | None = None) -> Any:
        # Policy violations raise TransportPolicyError here and are never retried.
        request = HttpRequest(
            url=self._policy.build_url(path, params),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout_s=self._timeout_s,
        )
        last_error: ExchangeError | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            self._limiter.acquire()
            try:
                resp = self._fetcher.get(request)
            except TransportError as exc:
                last_error = exc
                delay = self._retry.backoff(attempt)
            else:
                if resp.status == 200:
                    return _decode_json(resp.body)
                if resp.status not in self._retry.retry_statuses:
                    raise ExchangeHttpError(resp.status, _snippet(resp))
                error_type = RateLimitedError if resp.status == 429 else ExchangeHttpError
                last_error = error_type(resp.status, _snippet(resp))
                server_delay = self._retry.delay_for(attempt, resp.headers.get("retry-after"))
                if server_delay is None:  # server asks for longer than we accept: fail closed
                    raise last_error
                delay = server_delay
            if attempt < self._retry.max_attempts:
                self._sleep(delay)
        if isinstance(last_error, RateLimitedError):
            raise last_error
        raise ExchangeUnavailableError(
            f"{self.venue.value} {path}: giving up after {self._retry.max_attempts} attempts: "
            f"{last_error}"
        )
