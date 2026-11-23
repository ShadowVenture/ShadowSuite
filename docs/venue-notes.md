# Venue notes (public market data)

Facts the adapters rely on. They were recorded from the venues' public documentation and
public, unauthenticated probes on 2026-10-19. Venues can change behaviour without notice and
truncate silently, so re-check before relying on a limit. The recorded sample responses are in
`tests/fixtures/`.

## Coinbase Advanced Trade (public)

- REST: public market endpoints under `/api/v3/brokerage/market/...` plus `/time`; GET only.
- Candles: `start` and `end` are both inclusive, and at most 350 buckets per request. A larger
  window returns HTTP 400, except a window of exactly 350 intervals, which returns HTTP 200 and
  silently drops the oldest candle. The adapter sends `end` = open time of the last wanted
  bucket.
- WebSocket `level2` is full depth; the suite publishes the top 50 levels.
- Trade side semantics on the public trades channel are not documented unambiguously, so the
  aggressor is treated as unknown.
- Derivatives (INTX/CDE) metadata is parsed. Execution for Coinbase derivatives is disabled
  because mark, funding and settlement inputs are not verified.

## Crypto.com Exchange (public)

- REST: `/exchange/v1/public/get-(instruments|book|candlestick|trades|valuations)`; GET only.
- Candles: start inclusive, end exclusive, at most 300 per request. Asking for more, or
  omitting `count` (default 25), is silently truncated to the newest rows, so `count` is always
  sent and bounded.
- Valuations come newest first; the window is honoured with an exclusive end.
- Perpetual funding settles hourly (observed in `funding_hist` spacing).
- WebSocket `book.{instrument}.50` delivered 50x50 levels with no gaps during the probe. In the
  trade messages, `s` is the taker side. Mark prices come from the `mark.{instrument}` stream.
- No public REST rate limit is documented; the adapter stays well below typical per-IP limits.
