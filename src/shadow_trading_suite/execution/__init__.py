"""Deterministic PAPER execution and matching (M6-B). Nothing here contacts a venue.

Simulated orders exist only in the paper ledger. There is no live executor, no real order type
and no authenticated access; fills are `Literal[True]`-simulated ledger operations.
"""
