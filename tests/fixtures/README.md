# Test fixtures

## Recorded public API samples (`coinbase/`, `cryptocom/`, `ws/`)

These are short, trimmed samples of responses from the **public, unauthenticated market-data
APIs** of Coinbase Advanced Trade and Crypto.com Exchange: product and instrument metadata, a
few book levels, trades, candles and ticker messages. They contain no account data, no
credentials and no personal data. Some values were trimmed or shortened for the tests.

They exist only to test that the parsers accept the venues' published formats. They are factual
market data, and they are **not** licensed by this project under the GPL. If a rights holder
objects to their inclusion, open an issue and the samples will be replaced with synthetic data.

## Synthetic data

- `plugins/scripted.py`: test-only scripted plugins (fixed scripts, not strategies) used to
  exercise registration, signals, order requests, risk refusals, errors and stopping.
- `examples/scenarios/btc-usd-short.json` (in the repository root): synthetic prices; only its
  instrument metadata row is a recorded public product row.
