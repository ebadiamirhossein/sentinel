"""Comparing two reads of the same window (spike defect D-d, owner correction C3).

The spike found that Saxo's chart series is **not stable across queries**: the same
hour of the same instrument is present or absent depending on the request's anchor
and ``Count``. Sunday 19:00Z and 20:00Z appeared at ``From Fri 18:00Z, Count >= 50``
and were absent in six other framings. Reproducible, mechanism unknown, and
deliberately not guessed at.

That is why §4.4 forbids paging outright. It is also why the forex 1h tail asking
for 1200 bars instead of 321 — so the hour-of-day spread profile rests on ~35
samples per hour rather than ~10 — is a change that has to be **checked** rather than
assumed safe: if a 1200-bar read and a 321-bar read disagree about the bars they
share, then the features computed from the tail's last 321 rows are not the features
a 321-bar request would have produced, and the widening has quietly changed the
analysis as well as the spread profile.

This module is the comparison. It is pure, so the *detection* is testable offline;
the *fact* it detects can only be established against the live API, which is what
``tools/saxo_record_fixtures.py --check-tail-consistency`` exists to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sentinel.ingestion.models import Candle, OHLCVSeries

_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class TailDisagreement:
    """One way in which two reads of the same window differ."""

    at: datetime
    field: str
    long_value: Decimal | None
    short_value: Decimal | None

    def __str__(self) -> str:
        return (
            f"{self.at.isoformat()} {self.field}: long={self.long_value} short={self.short_value}"
        )


def compare_tail_overlap(
    long_tail: OHLCVSeries, short_tail: OHLCVSeries
) -> tuple[TailDisagreement, ...]:
    """Every difference between ``short_tail`` and the tail end of ``long_tail``.

    Compared over the window the two share, anchored on the **short** tail: that is
    the read whose result the feature engine would otherwise have seen, so it is the
    one whose every bar must be reproduced.

    A bar present in one and missing from the other is reported as a disagreement on
    the field ``present``, because that is the shape D-d actually takes — an hour
    that exists or does not depending on how you asked.

    An empty result means the two reads agree, and the 1200-bar tail can be trusted
    to contain the 321-bar one.
    """
    long_by_time = {candle.open_time: candle for candle in long_tail.candles}
    disagreements: list[TailDisagreement] = []

    for candle in short_tail.candles:
        counterpart = long_by_time.get(candle.open_time)
        if counterpart is None:
            disagreements.append(
                TailDisagreement(
                    at=candle.open_time, field="present", long_value=None, short_value=candle.close
                )
            )
            continue
        disagreements.extend(_field_differences(candle.open_time, counterpart, candle))

    overlap_start = short_tail.candles[0].open_time if short_tail.candles else None
    if overlap_start is not None:
        short_times = {candle.open_time for candle in short_tail.candles}
        for candle in long_tail.candles:
            if candle.open_time >= overlap_start and candle.open_time not in short_times:
                disagreements.append(
                    TailDisagreement(
                        at=candle.open_time,
                        field="present",
                        long_value=candle.close,
                        short_value=None,
                    )
                )

    return tuple(sorted(disagreements, key=lambda d: (d.at, d.field)))


def _field_differences(at: datetime, long: Candle, short: Candle) -> list[TailDisagreement]:
    found: list[TailDisagreement] = []
    for field in _FIELDS:
        long_value = getattr(long, field)
        short_value = getattr(short, field)
        if long_value != short_value:
            found.append(
                TailDisagreement(at=at, field=field, long_value=long_value, short_value=short_value)
            )
    return found


__all__ = ["TailDisagreement", "compare_tail_overlap"]
