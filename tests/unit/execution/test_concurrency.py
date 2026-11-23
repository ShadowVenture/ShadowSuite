"""Concurrency (§5.1, E1): concurrent bot requests are totally ordered by one writer; capital
and displayed liquidity are never spent twice."""

import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.execution.commands import CommandQueue
from tests.unit.execution.harness import PERP, Harness, limit_terms, market_terms

D = Decimal


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_queue_runs_commands_in_arrival_order_on_one_writer(h: Harness) -> None:
    h.perp_ready(asks=[("82001", "0.01")])
    queue = CommandQueue()
    order: list[str] = []

    def submit(bot: str, rid: str, lev: str) -> None:
        def command() -> object:
            order.append(rid)
            return h.accept(PERP, Side.BUY, "0.006", market_terms(), bot=bot, rid=rid, leverage=lev)

        queue.submit(command)

    threads = [
        threading.Thread(target=submit, args=(bot, f"{bot}-{n}", "3" if bot == "bot-04" else "5"))
        for n in range(3)
        for bot in ("bot-03", "bot-04")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert queue.drain() == 6
    assert len(order) == 6
    h.advance(600)
    plan = h.book(PERP, [("81999", "1")], [("82001", "0.01")])
    assert sum(D(f["quantity"]) for f in h.fills(plan)) == D("0.01")  # displayed size only
    h.verify_all()


def test_racing_threads_never_double_spend_capital(h: Harness) -> None:
    h.perp_ready()
    results = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        r = h.accept(PERP, Side.BUY, "0.05", limit_terms("80000"), rid=f"r{n}")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = [r for r in results if r.accepted]
    assert accepted  # some fit, the rest are refused by M5 or the ledger
    assert h.cash() >= 0
    total = sum((h.ledger.order(r.order_id).reserved for r in accepted if r.order_id), D(0))
    assert total == h.reserved()
    h.verify_all()
