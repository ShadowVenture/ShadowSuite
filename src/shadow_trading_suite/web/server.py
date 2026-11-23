"""Read-only local web UI: the suite's only inbound network endpoint.

- binds an IPv4 loopback address only (127.0.0.0/8), checked before and after binding;
- GET and HEAD only; every other method is answered 405 and nothing can be changed from it;
- no order, control, credential, wallet or fund route; no query string or body is interpreted;
- reads only the host's status files (`observability.status`), never the ledger;
- the Host header must name the bound address (DNS-rebinding defence);
- strict Content-Security-Policy (no scripts at all, same-origin stylesheet), no CORS.

Routes: `/` (HTML overview), `/style.css`, `/api/v1/status` (JSON), `/healthz`.
With no status file or no plugin it shows an explicit empty state, never zeros or "OK".
"""

import ipaddress
import json
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from shadow_trading_suite import __version__
from shadow_trading_suite.core.errors import SafetyViolationError
from shadow_trading_suite.observability.status import StatusDoc, StatusFileError, read_all_status

READ_METHODS = ("GET", "HEAD")
MAX_CONNECTIONS = 16
STALE_AFTER_S = 120
Response = tuple[int, str, bytes]

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; img-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)

CSS = b"""body{font:15px/1.45 system-ui,sans-serif;margin:0;background:#0f1216;color:#e6e9ee}
main{max-width:960px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0}
.banner{background:#3a2a00;color:#ffd78a;padding:8px 12px;border-radius:6px;margin:12px 0}
.card{background:#171b21;border:1px solid #2a3039;border-radius:8px;padding:12px 16px;margin:12px 0}
.empty{color:#a9b1bc}table{border-collapse:collapse;width:100%}td,th{text-align:left;
padding:6px 8px;border-bottom:1px solid #2a3039;vertical-align:top}code{color:#9fd0ff}
.state-failed{color:#ff8a8a}.state-running{color:#8affb5}.muted{color:#8a93a0}
@media (prefers-color-scheme:light){body{background:#f6f7f9;color:#14181d}
.card{background:#fff;border-color:#dde1e6}td,th{border-color:#dde1e6}.empty,.muted{color:#5b636e}}
"""


def require_loopback(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise SafetyViolationError(f"the web UI host must be an IP literal, got {host!r}") from exc
    if address.version != 4 or not address.is_loopback:
        raise SafetyViolationError(f"the web UI binds IPv4 loopback only, got {host!r}")
    return host


def _json(code: int, body: Mapping[str, Any]) -> Response:
    return code, "application/json", json.dumps(body, sort_keys=True, default=str).encode()


class WebApp:
    """Request handling without sockets (unit-testable): (method, path, headers) -> response."""

    def __init__(self, status_directory: Path, now: Callable[[], datetime]) -> None:
        self.status_directory = status_directory
        self.now = now
        self.allowed_hosts: frozenset[str] | None = None  # set by make_server

    def view(self) -> dict[str, Any]:
        docs = read_all_status(self.status_directory) if self.status_directory.is_dir() else {}
        now = self.now()
        out: dict[str, Any] = {
            "version": __version__,
            "paper_only": True,
            "live_trading": "not implemented",
            "runtime": None,
            "runtime_problem": "no runtime status file (the host has not run here yet)",
            "plugins": [],
        }
        runtime = docs.get("runtime")
        if isinstance(runtime, StatusFileError):
            out["runtime_problem"] = str(runtime)
        elif isinstance(runtime, StatusDoc):
            age = (now - runtime.produced_at).total_seconds()
            out["runtime"] = dict(runtime.payload) | {"status_age_s": round(age)}
            out["runtime_problem"] = (
                f"status is {round(age)} s old (stale)"
                if age > STALE_AFTER_S and runtime.payload.get("stopped_at") is None
                else None
            )
        plugins = docs.get("plugins")
        if isinstance(plugins, StatusDoc):
            out["plugins"] = list(plugins.payload.get("plugins", []))
        return out

    def respond(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        if method not in READ_METHODS:
            return _json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only UI: GET only"})
        hdrs = {k.lower(): v for k, v in headers.items()}
        if self.allowed_hosts is not None and hdrs.get("host") not in self.allowed_hosts:
            return _json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "unexpected Host header"})
        path = target.split("?", 1)[0]
        if path == "/healthz":
            return _json(HTTPStatus.OK, {"ok": True, "paper_only": True})
        if path == "/style.css":
            return HTTPStatus.OK, "text/css; charset=utf-8", CSS
        if path == "/api/v1/status":
            return _json(HTTPStatus.OK, self.view())
        if path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", render(self.view()).encode()
        return _json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def render(view: Mapping[str, Any]) -> str:
    e = escape
    runtime = view.get("runtime")
    plugins = view.get("plugins") or []
    rows = []
    for p in plugins:
        state = str(p.get("state", ""))
        rows.append(
            "<tr>"
            f"<td><code>{e(str(p.get('plugin_id')))}</code><br>"
            f"<span class=muted>{e(str(p.get('name')))} {e(str(p.get('version')))}</span></td>"
            f"<td>{e(str(p.get('venue')))}<br>"
            f"<span class=muted>{e(', '.join(map(str, p.get('instruments', []))))}</span></td>"
            f"<td class=state-{e(state)}>{e(state)}"
            + (f"<br><span class=muted>{e(str(p['error']))}</span>" if p.get("error") else "")
            + "</td>"
            f"<td>{e(json.dumps(p.get('counters', {}), sort_keys=True))}</td>"
            "</tr>"
        )
    if rows:
        table = (
            "<table><thead><tr><th>Bot plugin</th><th>Venue / instruments</th><th>State</th>"
            "<th>Requests</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        table = (
            '<p class="empty"><strong>No bots installed.</strong> The suite ships no trading '
            "bots. Register your own plugin as described in <code>docs/plugins.md</code>; "
            "nothing is loaded automatically.</p>"
        )
    if runtime is None:
        rt = f'<p class="empty">{e(str(view.get("runtime_problem")))}</p>'
    else:
        problem = view.get("runtime_problem")
        rt = (
            "<table><tbody>"
            f"<tr><th>Started</th><td>{e(str(runtime.get('started')))}</td></tr>"
            f"<tr><th>Stopped at</th><td>{e(str(runtime.get('stopped_at') or '-'))}</td></tr>"
            f"<tr><th>Portfolio</th><td>{e(str(runtime.get('portfolio_id')))}</td></tr>"
            f"<tr><th>Capital bootstrapped</th><td>{e(str(runtime.get('bootstrapped')))}</td></tr>"
            "<tr><th>Emergency shutdown</th>"
            f"<td>{e(str(runtime.get('emergency_shutdown')))}</td></tr>"
            f"<tr><th>Open paper orders</th><td>{e(str(runtime.get('open_orders')))}</td></tr>"
            f"<tr><th>Status age</th><td>{e(str(runtime.get('status_age_s')))} s"
            + (f" &middot; {e(str(problem))}" if problem else "")
            + "</td></tr></tbody></table>"
        )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Shadow Trading Suite</title><link rel=stylesheet href=/style.css></head><body>"
        "<main><h1>Shadow Trading Suite</h1>"
        f"<p class=muted>version {e(str(view.get('version')))}</p>"
        '<div class="banner">PAPER TRADING ONLY. Real order execution is not implemented. '
        "All capital shown is fictitious simulation money.</div>"
        f'<section class="card"><h2>Runtime</h2>{rt}</section>'
        f'<section class="card"><h2>Bots</h2>{table}</section>'
        "</main></body></html>"
    )


def _handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "shadow-trading-suite"
        sys_version = ""
        timeout = 5

        def _serve(self) -> None:
            code, ctype, body = app.respond(self.command, self.path, dict(self.headers.items()))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            if code == HTTPStatus.METHOD_NOT_ALLOWED:
                self.send_header("Allow", ", ".join(READ_METHODS))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _serve  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:
            return  # no access log (no client data is stored)

    return Handler


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(host: str, port: int, app: WebApp) -> HTTPServer:
    require_loopback(host)
    server = _BoundedServer((host, port), _handler(app))
    bound = str(server.server_address[0])
    if not ipaddress.ip_address(bound).is_loopback:  # defence in depth
        server.server_close()
        raise SafetyViolationError(f"the web UI bound a non-loopback address {bound}")
    bound_port = int(server.server_address[1])
    app.allowed_hosts = frozenset({f"{bound}:{bound_port}", f"localhost:{bound_port}"})
    return server
