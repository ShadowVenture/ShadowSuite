# Glossary of internal design labels

The suite was extracted from a larger private project. Code comments and test names keep some
of that project's design labels. The behaviour they refer to is specified by the code and the
tests in this repository. The internal design notes themselves are not published.

| Label | Meaning |
|---|---|
| M5 | the risk layer: `risk/` (per-bot risk engine, portfolio supervisor, capital manager, audit) |
| M6-A | the double-entry trading ledger: `accounting/`, `storage/ledger.py` |
| M6-B | the paper execution engine: `execution/` |
| M6-C | the paper runtime layer: `paper/` (EUR rates, volatility, equity snapshots); in the suite, `host/` takes this role |
| M7, M7-A | versioned risk rules (`risk.engine.RULES_VERSION`); v2 makes a spot SELL exposure-reducing |
| E1-E9 | execution rules (E1 single writer / FIFO queue, E2 and E8 matching rules, E4 gap/restart cancellation, E9 instrument support matrix) |
| B5, B6 | funding settlement order (cash, then margin, then payable) and liquidation price band rules |
| R1-R8, C11, C13, X5 | design requirements: R1 economic equity for risk, R7 liquidation and funding model, R8 fee and margin assumptions, C11/X5 real-time EUR conversion, C13 published book depth |
| §n | sections of the internal design notes for the ledger and execution engine |
| ASSUMPTION | a simulation parameter that has not been verified against an account or venue |
| VERIFIED | a venue fact with recorded public evidence (see [venue-notes.md](venue-notes.md)) |
