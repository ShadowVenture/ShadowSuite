"""WebSocket chokepoint: exact public endpoints, guarded outbound frames, safe connect options."""

import asyncio
from typing import Any

import pytest

from shadow_trading_suite.core.errors import SafetyViolationError
from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.exchanges.errors import ResponseFormatError, TransportPolicyError
from shadow_trading_suite.streaming.transport import (
    ALLOWED_WS_URLS,
    WS_MAX_MESSAGE_BYTES,
    GuardedConnection,
    WebsocketsConnector,
    WsClosedError,
    WsEndpoint,
)


def _accept_only_ping(text: str) -> None:
    if text != "ping":
        raise TransportPolicyError("only ping allowed")


class _Inner:
    def __init__(self, incoming: list[Any] | None = None) -> None:
        self.sent: list[str] = []
        self.incoming = incoming or []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> Any:
        return self.incoming.pop(0)

    async def close(self) -> None:
        pass


class TestEndpoints:
    def test_public_market_endpoints_only(self) -> None:
        assert ALLOWED_WS_URLS == {
            Venue.COINBASE: "wss://advanced-trade-ws.coinbase.com",
            Venue.CRYPTOCOM: "wss://stream.crypto.com/exchange/v1/market",
        }

    @pytest.mark.parametrize(
        "url",
        [
            "wss://stream.crypto.com/exchange/v1/user",  # authenticated user API
            "wss://advanced-trade-ws-user.coinbase.com",  # authenticated user channel host
            "ws://stream.crypto.com/exchange/v1/market",
            "wss://stream.crypto.com/exchange/v1/market?token=x",
            "wss://evil.example/exchange/v1/market",
        ],
    )
    def test_other_urls_refused(self, url: str) -> None:
        with pytest.raises(TransportPolicyError):
            WsEndpoint(venue=Venue.CRYPTOCOM, url=url, validate_outbound=_accept_only_ping)

    def test_policy_errors_are_safety_violations(self) -> None:
        assert issubclass(TransportPolicyError, SafetyViolationError)


class TestGuardedConnection:
    endpoint = WsEndpoint(
        venue=Venue.COINBASE,
        url=ALLOWED_WS_URLS[Venue.COINBASE],
        validate_outbound=_accept_only_ping,
    )

    def test_allowed_frame_sent(self) -> None:
        inner = _Inner()
        asyncio.run(GuardedConnection(inner, self.endpoint).send("ping"))
        assert inner.sent == ["ping"]

    def test_disallowed_frame_never_reaches_the_socket(self) -> None:
        inner = _Inner()
        with pytest.raises(TransportPolicyError):
            asyncio.run(GuardedConnection(inner, self.endpoint).send('{"method": "auth"}'))
        assert inner.sent == []

    def test_binary_frames_rejected(self) -> None:
        conn = GuardedConnection(_Inner([b"\x00\x01"]), self.endpoint)
        with pytest.raises(ResponseFormatError, match="text"):
            asyncio.run(conn.recv())


class TestConnectorOptions:
    def test_safe_options_and_failure_mapping(self) -> None:
        captured: dict[str, Any] = {}

        async def fake_connect(url: str, **kwargs: Any) -> _Inner:
            captured.update(url=url, **kwargs)
            return _Inner()

        endpoint = WsEndpoint(
            venue=Venue.CRYPTOCOM,
            url=ALLOWED_WS_URLS[Venue.CRYPTOCOM],
            validate_outbound=_accept_only_ping,
        )
        conn = asyncio.run(WebsocketsConnector(connect=fake_connect).connect(endpoint))
        assert isinstance(conn, GuardedConnection)
        assert captured["url"] == ALLOWED_WS_URLS[Venue.CRYPTOCOM]
        assert captured["proxy"] is None  # never pick up proxies from the environment
        assert captured["compression"] is None
        assert captured["max_size"] == WS_MAX_MESSAGE_BYTES
        assert captured["additional_headers"] is None
        assert captured["origin"] is None
        assert captured["ping_interval"]
        assert captured["ping_timeout"]
        assert "paper-only" in captured["user_agent_header"]

        async def failing(url: str, **kwargs: Any) -> _Inner:
            raise OSError("network unreachable")

        with pytest.raises(WsClosedError, match="unreachable"):
            asyncio.run(WebsocketsConnector(connect=failing).connect(endpoint))
