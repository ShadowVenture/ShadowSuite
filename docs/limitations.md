# Known limitations (0.1.0.dev1)

- **Paper only.** No live trading, no private exchange data, no credentials (by design for this
  version).
- **No live public data mode in the CLI.** The public REST/WebSocket adapters are implemented
  and tested offline, but `sts` runs only offline scenarios. A `sts run` live mode is on the
  roadmap.
- **Spot only for plugins.** Derivatives are modelled in the risk and execution layers but not
  admitted for plugins.
- **EUR base currency** with rates triangulated from Coinbase tickers. Other base currencies are
  not supported.
- **Jurisdiction policy.** `instruments/eligibility.py` ships one conservative example policy
  (Germany/EU). It is informational and is not used to admit plugins. Listing on an exchange
  never implies that an instrument is tradable for you.
- **Fees, slippage and the maker queue model are ASSUMPTIONS**, not verified against an account.
- **Plugins run in-process** and are not sandboxed (see [security.md](security.md)).
- **Single process, single writer.** One host owns one ledger; there is no multi-host
  coordination.
- **No persistent kill switch file and no historical data store or backtester** yet.
- Simulated results are not evidence of profitability. Nothing here is financial advice.
