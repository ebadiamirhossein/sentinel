"""The Persian summary path (M11p) -- a convenience surface, not an analysis one.

A button on a card sends the owner a short, friendly Persian rewrite of that card.
Two properties make it safe to bolt onto a system that sizes real money, and both
live in this package:

1. **The model sees the card and nothing else.** Not features, not charts, not the
   structured analyst report, not a database row. It cannot form a different opinion
   because it never sees the data -- only the words the analyst already wrote.
2. **Every number in the output must already be in the input**, and the verdict line
   must agree with the card's verdict. The prompt asks;
   :mod:`sentinel.analyst.persian.numbers` *checks* both, and both fail closed. A
   prompt instruction is a request; a check is a rail.

Nothing here imports from ``sentinel.risk``, ``sentinel.tracker`` or
``sentinel.stats``, and nothing in those packages imports from here. The feature is
removable without any of them noticing.
"""

from __future__ import annotations

from sentinel.analyst.persian.models import PersianSourceKind, PersianSummary
from sentinel.analyst.persian.numbers import (
    NumberCheck,
    VerdictCheck,
    VerdictClass,
    check_numbers,
    check_verdict,
    numeric_tokens,
    plain_text,
)
from sentinel.analyst.persian.summariser import (
    PersianSummariser,
    SummaryOutcome,
    SummaryResult,
)

__all__ = [
    "NumberCheck",
    "PersianSourceKind",
    "PersianSummariser",
    "PersianSummary",
    "SummaryOutcome",
    "SummaryResult",
    "VerdictCheck",
    "VerdictClass",
    "check_numbers",
    "check_verdict",
    "numeric_tokens",
    "plain_text",
]
