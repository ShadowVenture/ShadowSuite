"""Property tests: capital invariants under arbitrary operation sequences.

The former profit-reserve split property was removed with the reserve itself."""

import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.risk.capital import CapitalManager, InsufficientCapitalError
from shadow_trading_suite.storage.ledger import LedgerStore

D = Decimal
NOW = datetime(2026, 10, 19, tzinfo=UTC)
amounts = st.integers(min_value=50, max_value=250).map(D)  # big enough to hit the cap
ops = st.lists(
    st.one_of(
        st.tuples(st.just("commit"), amounts),
        st.tuples(st.just("release"), st.integers(min_value=0, max_value=20)),
        st.tuples(st.just("gain"), amounts),
    ),
    max_size=25,
)


@settings(max_examples=25, deadline=None)
@given(sequence=ops)
def test_capital_never_overcommitted_and_replay_is_exact(
    sequence: list[tuple[str, object]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = ManualClock(NOW)
        store = LedgerStore.open(Path(tmp) / "data", clock=clock)
        try:
            capital = CapitalManager(store, clock, D("0.002"))
            capital.deposit_paper_capital("seed", D(1500))
            # small balance (~USD 348) so random commits regularly reach the cap
            capital.allocate("alloc", "bot-03", Venue.CRYPTOCOM, "USD", D(300), D("0.86"))
            refs: list[str] = []
            for i, (kind, value) in enumerate(sequence):
                if kind == "commit":
                    assert isinstance(value, Decimal)
                    try:
                        capital.commit(f"r{i}", "bot-03", Venue.CRYPTOCOM, "USD", value)
                        refs.append(f"r{i}")
                    except InsufficientCapitalError:
                        pass
                elif kind == "release" and refs:
                    assert isinstance(value, int)
                    capital.release(refs.pop(value % len(refs)))
                elif kind == "gain":
                    assert isinstance(value, Decimal)
                    capital.realize(f"g{i}", "bot-03", Venue.CRYPTOCOM, "USD", value)
                acct = capital.account("bot-03", Venue.CRYPTOCOM, "USD")
                assert D(0) <= acct.committed <= acct.balance
                assert acct.free >= 0
            replayed = CapitalManager(store, clock, D("0.002"))
            assert replayed.state_digest() == capital.state_digest()
        finally:
            store.close()
