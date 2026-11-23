"""Deterministic test doubles: fake time and a scripted fetcher. No network."""

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from shadow_trading_suite.exchanges.http import HttpRequest, HttpResponse

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def fixture_bytes(rel: str) -> bytes:
    return (FIXTURES / rel).read_bytes()


def fixture_json(rel: str) -> Any:
    return json.loads(fixture_bytes(rel))


class FakeTime:
    """Monotonic clock whose sleep() advances time instantly and records each call."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


Step = HttpResponse | BaseException


class ScriptedFetcher:
    """Returns scripted responses in order, or routes by URL path via `routes`."""

    def __init__(
        self,
        script: Iterable[Step] = (),
        routes: dict[str, Callable[[dict[str, str]], Step]] | None = None,
    ) -> None:
        self.script = list(script)
        self.routes = routes or {}
        self.requests: list[HttpRequest] = []

    def get(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urlsplit(request.url)
        if parts.path in self.routes:
            step = self.routes[parts.path](dict(parse_qsl(parts.query)))
        else:
            assert self.script, f"unexpected request {request.url}"
            step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def params(self) -> list[dict[str, str]]:
        return [dict(parse_qsl(urlsplit(r.url).query)) for r in self.requests]


def ok(body: bytes | str, headers: dict[str, str] | None = None) -> HttpResponse:
    raw = body.encode() if isinstance(body, str) else body
    return HttpResponse(
        status=200, headers=headers or {"content-type": "application/json"}, body=raw
    )


def status(code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=code, headers=headers or {}, body=body)
