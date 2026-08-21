"""What a market is, and nothing else (M10a).

A **leaf module on purpose.** ``storage``, ``stats``, ``bot``, ``llm`` and ``core``
all need to name a market, and every one of them already imports from at least one
of the others. Putting :class:`Market` anywhere that imports something would create
a cycle within a milestone or two; putting it here — with no project imports at all
— means it can be imported from anywhere, for ever. Same discipline as
``bot/models.py``, which journal/M6_REPORT.md §7 records for the same reason.

The value is a **string** on the wire and in the database, because that is what a
``String(16)`` column and a JSONB payload hold. ``StrEnum`` gives that for free
while still failing loudly on a typo, which a bare ``str`` would not.

M10a introduced ``FOREX`` and implemented **none of it**: the member existed so the
schema, the config and the statistics could carry the dimension before the adapter
did. M10b-1 adds the adapter and the deterministic forex modules under
``sentinel/fx/``, and still wires **no cycle** to them — the orchestrator builds only
the Binance adapter. A forex row can therefore be written by a test and by nothing
else, which is what ``markets.forex.enabled: false`` is meant to guarantee.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Market(StrEnum):
    """Which market a row, a cycle, a budget or a pause belongs to.

    Closed by construction. A fifth population dimension (M10a Step 6) and a
    per-market budget (Step 4) are only as trustworthy as the set of markets being
    finite and known, exactly as ``RejectionReason`` and ``SkipReason`` are.
    """

    CRYPTO = "crypto"
    FOREX = "forex"


#: The market every row created before M10a belongs to, and the value migration
#: ``0010`` backfills. Named rather than spelled ``Market.CRYPTO`` at each call
#: site so "this is the historical default" and "this happens to be crypto" stay
#: distinguishable when forex arrives.
LEGACY_MARKET: Final[Market] = Market.CRYPTO

#: How wide the database column is. One constant, so the ORM model, the migration
#: and any future table agree without anybody remembering a number.
MARKET_COLUMN_LENGTH: Final[int] = 16


__all__ = ["LEGACY_MARKET", "MARKET_COLUMN_LENGTH", "Market"]
