"""Forex coherence checks (FOREX.md §16.5 row 5) — one pure function per rule.

A near-twin of :mod:`sentinel.risk.coherence`, and a **copy rather than an import**
for the reason ``sentinel/fx/rounding.py`` is: that package is frozen for the live
measurement window, and coupling new-market checks to a module nobody may touch means
the day crypto's rules change on evidence, forex's change with them silently.

Two things genuinely differ, and neither is stylistic:

* the bound in :func:`check_entry_distance` is **``ForexConfig.max_entry_distance_pct``
  = 0.5**, not crypto's 3.0. Spec defect #21: EURUSD moves about 0.5% in a day, so a 3%
  bound could essentially never fire here, and a rail that cannot fire is worse than an
  absent one because it reads on a checklist as a rail;
* every distance is also available in **pips**, because that is the unit this market is
  discussed in and a four-decimal percentage is not readable at 3am.

The ATR-multiple bounds are carried over unchanged, and that is a decision rather than
an omission: they are expressed in ATR, which is already the instrument's own
volatility, and that is exactly what makes them transferable where a percentage is not.

Order matters and is fixed by :mod:`sentinel.fx.gate`, so a rejection always names the
**first** thing wrong rather than a downstream symptom.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from sentinel.analyst.models import Direction, EntryZone
from sentinel.fx.models import ForexRejection

HUNDRED = Decimal("100")


def check_geometry(
    *,
    direction: Direction,
    zone: EntryZone,
    stop: Decimal,
    targets: tuple[Decimal, ...],
) -> ForexRejection | None:
    """Long: ``stop < low < high`` and every target above the zone. Short: mirrored."""
    if zone.low <= 0 or zone.low >= zone.high or stop <= 0 or any(t <= 0 for t in targets):
        return ForexRejection.ENTRY_ZONE_INVALID

    if direction is Direction.LONG:
        if stop >= zone.low:
            return ForexRejection.STOP_SIDE
        if any(target <= zone.high for target in targets):
            return ForexRejection.TARGET_ORDER
        if not _monotonic(targets, ascending=True):
            return ForexRejection.TARGET_ORDER
        return None

    if stop <= zone.high:
        return ForexRejection.STOP_SIDE
    if any(target >= zone.low for target in targets):
        return ForexRejection.TARGET_ORDER
    if not _monotonic(targets, ascending=False):
        return ForexRejection.TARGET_ORDER
    return None


def _monotonic(targets: tuple[Decimal, ...], *, ascending: bool) -> bool:
    return all((b > a) if ascending else (b < a) for a, b in pairwise(targets))


def check_entry_distance(
    *, zone: EntryZone, last_price: Decimal, max_distance_pct: Decimal
) -> ForexRejection | None:
    """**Both** edges must be inside the budget, so every rung is a plausible fill.

    This is the rule spec defect #21 is about. The budget here is a forex number.
    """
    if last_price <= 0:
        return ForexRejection.ENTRY_TOO_FAR

    worst = max(abs(zone.high - last_price), abs(zone.low - last_price))
    if worst / last_price * HUNDRED > max_distance_pct:
        return ForexRejection.ENTRY_TOO_FAR
    return None


def check_stop_distance(
    *,
    avg_entry: Decimal,
    stop: Decimal,
    atr: Decimal,
    min_multiple: Decimal,
    max_multiple: Decimal,
) -> ForexRejection | None:
    """Measured from the weighted average entry, the same basis as every RR figure.

    Below ``min_multiple`` x ATR the stop sits inside the noise and guarantees a
    stop-out — and in forex it is also where the spread hurts most (§7.2). Above
    ``max_multiple`` x ATR it is a lazy stop that wrecks the RR, and at €200 it is also
    where ``BELOW_MIN_TICKET`` starts firing, because a wider stop needs a *smaller*
    position.
    """
    distance = abs(avg_entry - stop)
    if distance < min_multiple * atr:
        return ForexRejection.STOP_TOO_TIGHT
    if distance > max_multiple * atr:
        return ForexRejection.STOP_TOO_WIDE
    return None


def rr_multiples(
    *, avg_entry: Decimal, stop: Decimal, targets: tuple[Decimal, ...]
) -> tuple[Decimal, ...]:
    """Reward-to-risk **gross** for each target, from the weighted average entry."""
    risk = abs(avg_entry - stop)
    if risk <= 0:
        raise ValueError("reward-to-risk is undefined with a zero stop distance")
    return tuple(abs(target - avg_entry) / risk for target in targets)


def check_rr(*, rr: tuple[Decimal, ...], min_rr_tp1: Decimal) -> ForexRejection | None:
    """Only TP1 is gated; later targets are informational.

    This is the **gross** check. It exists separately from the net one so that "the
    analyst proposed a poor RR" and "the setup was fine and the spread ate it" stay
    distinguishable — they call for different actions, and §7.4's whole point is that
    the second happens far more often in this market than the arithmetic suggests.
    """
    if rr[0] < min_rr_tp1:
        return ForexRejection.RR_TOO_LOW
    return None


def check_confidence(*, confidence: int, min_confidence: int) -> ForexRejection | None:
    """Below the bar the idea is **downgraded** to watchlist, never rejected."""
    if confidence < min_confidence:
        return ForexRejection.LOW_CONFIDENCE
    return None


__all__ = [
    "check_confidence",
    "check_entry_distance",
    "check_geometry",
    "check_rr",
    "check_stop_distance",
    "rr_multiples",
]
