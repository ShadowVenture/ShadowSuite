# Paper trading

Everything in the suite is a **simulation**. The capital is fictitious, and orders and fills
exist only in the local paper ledger.

## Capital

- `starting_capital_eur` in the portfolio file is the fictitious paper capital, held in EUR.
- On the first fresh, live EUR rate the host books the starting capital and allocates each
  `[[bots]] allocation_eur` to that bot, in the venue's quote currency at the live rate.
  Before that, every BUY is refused ("paper capital not bootstrapped").
- The EUR rate is triangulated from public tickers (BTC-EUR/BTC-USD, checked against
  ETH-EUR/ETH-USD). If the rate is missing, stale or inconsistent, nothing is valued and new
  exposure is refused.

## Risk inputs (fail closed)

For each request the host derives the inputs from the ledger:

- economic equity at current marks;
- durable 00:00 UTC day-start and peak equity;
- the order count of the last minute;
- minute-close volatility.

Volatility needs at least 30 one-minute returns. If any input is missing or stale, the request
is refused. A spot SELL needs no volatility estimate.

## Risk controls

Every bot needs a `[[bots]]` profile:

| Field | Meaning |
|---|---|
| `allocation_eur` | paper capital for this bot |
| `max_position_margin`, `max_total_margin` | fractions of the bot's equity |
| `max_leverage`, `max_effective_leverage` | hypothetical simulation caps; spot is always 1x |
| `min_liquidation_distance` | derivatives only (not admitted for plugins in 0.1) |
| `daily_loss_limit`, `max_drawdown` | fractions; breaches block new exposure |
| `max_open_positions`, `max_orders_per_minute` | counts |

Portfolio limits (`[limits]`) cover gross exposure per underlying, venue and in total,
instrument concentration, stress loss under price shocks, emergency daily loss and emergency
drawdown. On an emergency breach the supervisor enters **emergency shutdown**: every exposure
increase is refused and every plugin is stopped. Exits stay possible.

## Simulated execution

- An accepted order is booked in the ledger with reserved collateral (fees included).
- Fills happen only on a **later** market event after a deterministic simulated latency. They
  need a fresh, LIVE book and stay within the displayed, not yet consumed depth and a
  protection price band.
- Maker fills use a queue model. It is an **ASSUMPTION** and has never been verified against a
  venue.
- If a venue's streams go down or stale past the gap threshold, or the host restarts, resting
  orders are cancelled.
- Fees and conversion costs in `[fees]` are **ASSUMPTIONS**. Verify them against the official
  fee schedule of the venue you model.
- Every command is one atomic ledger unit of work with a hash-chained audit record.
  `verify_replay()` recomputes the state from the ledger operations.

## What simulation is not

A paper fill is not proof that a real order would have filled at that price. Simulated profits
are not evidence of profitability. See [limitations.md](limitations.md).
