"""The WebSocket network chokepoint (the only module allowed to import `websockets`).

- Only the exact public market-data endpoints of each venue can be opened.
- Every outbound frame passes the venue's outbound validator before it reaches the socket
  (subscribe / unsubscribe / heartbeat replies only; no auth, no private methods).
- Environment proxies and compression are disabled; frame size and queue are bounded;
  protocol-level ping/pong detects dead connections.
"""

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import connect as websockets_connect
from websockets.exceptions import WebSocketException

from shadow_trading_suite import __version__
from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.exchanges.errors import (
    ResponseFormatError,
    TransportError,
    TransportPolicyError,
)

ALLOWED_WS_URLS: dict[Venue, str] = {
    Venue.COINBASE: "wss://advanced-trade-ws.coinbase.com",
    Venue.CRYPTOCOM: "wss://stream.crypto.com/exchange/v1/market",
}
WS_MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # Coinbase level2 snapshot observed at ~4.5 MB
WS_MAX_QUEUE = 1024
USER_AGENT = f"shadow-trading-suite/{__version__} (paper-only; public market data)"


class WsClosedError(TransportError):
    """The connection failed or closed. Streams on it are no longer valid."""


OutboundValidator = Callable[[str], None]


@dataclass(frozen=True)
class WsEndpoint:
    venue: Venue
    url: str
    validate_outbound: OutboundValidator

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if self.url != ALLOWED_WS_URLS.get(self.venue) or parts.scheme != "wss" or parts.query:
            raise TransportPolicyError(
                f"{self.venue.value}: websocket url {self.url!r} not allowed"
            )


class RawConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> Any: ...
    async def close(self) -> None: ...


class WsConnection(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class WsConnector(Protocol):
    async def connect(self, endpoint: WsEndpoint) -> WsConnection: ...


class GuardedConnection:
    def __init__(self, inner: RawConnection, endpoint: WsEndpoint) -> None:
        self._inner = inner
        self._endpoint = endpoint

    async def send(self, text: str) -> None:
        self._endpoint.validate_outbound(text)  # raises TransportPolicyError
        try:
            await self._inner.send(text)
        except (WebSocketException, OSError) as exc:
            raise WsClosedError(f"send failed: {exc}") from exc

    async def recv(self) -> str:
        try:
            frame = await self._inner.recv()
        except (WebSocketException, OSError) as exc:
            raise WsClosedError(f"connection lost: {exc}") from exc
        if not isinstance(frame, str):
            raise ResponseFormatError("expected a text frame")
        return frame

    async def close(self) -> None:
        with contextlib.suppress(WebSocketException, OSError):
            await self._inner.close()


ConnectFn = Callable[..., Awaitable[RawConnection]]


async def _default_connect(url: str, **kwargs: Any) -> RawConnection:
    connection: RawConnection = await websockets_connect(url, **kwargs)
    return connection


class WebsocketsConnector:
    def __init__(self, connect: ConnectFn = _default_connect) -> None:
        self._connect = connect

    async def connect(self, endpoint: WsEndpoint) -> WsConnection:
        try:
            raw = await self._connect(
                endpoint.url,
                proxy=None,
                compression=None,
                max_size=WS_MAX_MESSAGE_BYTES,
                max_queue=WS_MAX_QUEUE,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                user_agent_header=USER_AGENT,
                additional_headers=None,
                origin=None,
            )
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise WsClosedError(f"{endpoint.venue.value}: connect failed: {exc}") from exc
        return GuardedConnection(raw, endpoint)
