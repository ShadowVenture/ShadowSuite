# Roadmap

Nothing on this list is implemented. Items are not promises and have no dates. Live order
execution is deliberately **not** on the near-term list.

## Near term (still paper only)

- `sts run`: live **public** market-data mode. It wires the existing WebSocket sessions and REST
  discovery into the host, with fail-closed handling of gaps and stale data.
- Persistent kill switches (global and per bot), readable by the host on start, and an operator
  CLI to stop plugins in a running host.
- Plugin process isolation (a separate process per plugin with a narrow IPC channel) and
  resource limits.
- Admission of derivatives (perpetuals) for plugins in simulation, after mark, funding and
  margin inputs are wired and tested.
- A historical data store and a backtest runner that use the same plugin contract.
- Configurable base currency and jurisdiction policies.
- Optional independent hard caps (guard layer) in front of the risk engine.

## Later, only after separate review

- **Live trading is not implemented.** Any real order path would need, at minimum:
  - a separate, audited order transport;
  - read-only verification of account data;
  - explicit per-venue legal and eligibility review;
  - independent hard limits and kill switches;
  - a durable order journal with reconciliation;
  - an explicit, documented opt-in.

  None of this exists. The current safety tests forbid it, and they would have to be changed
  deliberately and visibly.
