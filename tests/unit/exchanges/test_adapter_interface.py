"""C4: both venues implement the read-only ExchangeAdapter interface — and nothing more."""

import inspect

import pytest

from shadow_trading_suite.exchanges.base import ExchangeAdapter, MarketDataView
from shadow_trading_suite.exchanges.coinbase import CoinbaseAdapter
from shadow_trading_suite.exchanges.cryptocom import CryptoComAdapter
from tests.unit.exchanges.fakes import ScriptedFetcher

READ_ONLY_ADAPTER_MEMBERS = {
    "venue",
    "list_instruments",
    "get_candles",
    "get_order_book",
    "get_recent_trades",
    "get_funding_rate",
    "get_funding_history",
    "get_mark_prices",
    "get_index_prices",
    "capabilities",
    "limits",
}

# Static conformance: mypy (strict, run on tests/) rejects these assignments if either
# adapter stops matching the protocol.
_coinbase: ExchangeAdapter = CoinbaseAdapter(fetcher=ScriptedFetcher())
_cryptocom: ExchangeAdapter = CryptoComAdapter(fetcher=ScriptedFetcher())


def _public_members(obj: object) -> set[str]:
    return {name for name, _ in inspect.getmembers(obj) if not name.startswith("_")}


def test_protocol_declares_only_read_only_members() -> None:
    assert _public_members(ExchangeAdapter) - set(dir(object)) == READ_ONLY_ADAPTER_MEMBERS


@pytest.mark.parametrize("adapter_cls", [CoinbaseAdapter, CryptoComAdapter])
def test_adapters_expose_exactly_the_read_only_interface(adapter_cls: type) -> None:
    assert _public_members(adapter_cls) == READ_ONLY_ADAPTER_MEMBERS


def test_market_data_view_excludes_discovery() -> None:
    members = _public_members(MarketDataView) - set(dir(object))
    assert members == READ_ONLY_ADAPTER_MEMBERS - {"list_instruments"}


def test_old_name_is_gone() -> None:
    import shadow_trading_suite.exchanges.base as base

    assert not hasattr(base, "MarketDataAdapter")
