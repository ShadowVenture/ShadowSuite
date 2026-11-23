import pytest

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.exchanges.errors import TransportPolicyError
from shadow_trading_suite.exchanges.policy import EndpointPolicy

POLICY = EndpointPolicy(
    venue=Venue.CRYPTOCOM,
    base_url="https://api.example.com",
    path_patterns=(r"/v1/public/get-(book|trades)", r"/v1/market/products/[A-Z0-9-]+"),
    allowed_params=frozenset({"instrument_name", "depth"}),
)


class TestAllowedRequests:
    def test_builds_url_with_sorted_params(self) -> None:
        url = POLICY.build_url("/v1/public/get-book", {"instrument_name": "BTC_USD", "depth": 5})
        assert url == "https://api.example.com/v1/public/get-book?depth=5&instrument_name=BTC_USD"

    def test_no_params(self) -> None:
        assert POLICY.build_url("/v1/market/products/BTC-USD") == (
            "https://api.example.com/v1/market/products/BTC-USD"
        )

    def test_odd_but_valid_symbol_value(self) -> None:
        POLICY.build_url("/v1/public/get-book", {"instrument_name": "SHIB_BTC@OEX_HK"})


class TestRejectedRequests:
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/private/create-order",
            "/v1/public/get-book/../../private/x",
            "/v1/public/get-books",
            "/v1/public/get-book/",
            "/v1/market/products/BTC-USD/../orders",
            "/v1/market/products/btc usd",
            "/api/v3/brokerage/orders",
            "",
            "v1/public/get-book",
        ],
    )
    def test_non_allowlisted_paths(self, path: str) -> None:
        with pytest.raises(TransportPolicyError, match="path"):
            POLICY.build_url(path)

    @pytest.mark.parametrize("name", ["api_key", "sig", "nonce", "signature", "token", "secret"])
    def test_non_allowlisted_params(self, name: str) -> None:
        with pytest.raises(TransportPolicyError, match="parameter"):
            POLICY.build_url("/v1/public/get-book", {"instrument_name": "BTC_USD", name: "x"})

    @pytest.mark.parametrize("value", ["", "a b", "x&api_key=1", "../x", "a" * 65, "é"])
    def test_unsafe_param_values(self, value: str) -> None:
        with pytest.raises(TransportPolicyError, match="value"):
            POLICY.build_url("/v1/public/get-book", {"instrument_name": value})

    def test_bool_param_value_rejected(self) -> None:
        with pytest.raises(TransportPolicyError, match="value"):
            POLICY.build_url("/v1/public/get-book", {"depth": True})

    @pytest.mark.parametrize(
        "base",
        ["http://api.example.com", "https://api.example.com/v1", "https://u:p@api.example.com"],
    )
    def test_base_url_must_be_bare_https_origin(self, base: str) -> None:
        with pytest.raises(TransportPolicyError):
            EndpointPolicy(
                venue=Venue.COINBASE,
                base_url=base,
                path_patterns=("/x",),
                allowed_params=frozenset(),
            )
