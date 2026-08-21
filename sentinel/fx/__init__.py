"""Forex-only deterministic logic (M10b, docs/specs/FOREX.md).

A **new package, never an edit to** ``sentinel/risk/``. The crypto risk engine is
frozen for the duration of the live measurement window, and forex arithmetic is
different arithmetic anyway: pips instead of ticks, a minimum *trade size* instead
of a minimum notional, account-level margin instead of a per-position liquidation
price, and a measured spread instead of an order book.

Nothing here imports an LLM client, opens a socket or reads a database. It is the
same discipline ``sentinel/risk/`` keeps, applied to the second market.
"""
