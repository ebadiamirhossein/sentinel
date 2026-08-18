"""§2 coherence checks — one pure function per rule, each returning a code or None.

Order matters and is fixed by :mod:`sentinel.risk.engine`: geometry before
distance before confidence, so a rejection always names the *first* thing wrong
rather than a downstream symptom.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from sentinel.analyst.models import Direction, EntryZone
from sentinel.risk.models import RejectionReason

HUNDRED = Decimal("100")


def check_geometry(
    *,
    direction: Direction,
    zone: EntryZone,
    stop: Decimal,
    targets: tuple[Decimal, ...],
) -> RejectionReason | None:
    """Rule 1. Long: ``stop < low < high`` and every target above the zone. Short: mirrored."""
    if zone.low <= 0 or zone.low >= zone.high or stop <= 0 or any(t <= 0 for t in targets):
        return RejectionReason.ENTRY_ZONE_INVALID

    if direction is Direction.LONG:
        if stop >= zone.low:
            return RejectionReason.STOP_SIDE
        if any(target <= zone.high for target in targets):
            return RejectionReason.TARGET_ORDER
        if not _monotonic(targets, ascending=True):
            return RejectionReason.TARGET_ORDER
        return None

    if stop <= zone.high:
        return RejectionReason.STOP_SIDE
    if any(target >= zone.low for target in targets):
        return RejectionReason.TARGET_ORDER
    if not _monotonic(targets, ascending=False):
        return RejectionReason.TARGET_ORDER
    return None


def _monotonic(targets: tuple[Decimal, ...], *, ascending: bool) -> bool:
    pairs = pairwise(targets)
    return all((b > a) if ascending else (b < a) for a, b in pairs)


def check_entry_distance(
    *, zone: EntryZone, last_price: Decimal, max_distance_pct: Decimal
) -> RejectionReason | None:
    """Rule 2, owner ruling: **both** edges must be inside the budget, so every
    rung is a plausible fill rather than a fantasy one. A non-positive last price
    makes the check unanswerable, which is itself a rejection."""
    if last_price <= 0:
        return RejectionReason.ENTRY_TOO_FAR

    worst = max(abs(zone.high - last_price), abs(zone.low - last_price))
    if worst / last_price * HUNDRED > max_distance_pct:
        return RejectionReason.ENTRY_TOO_FAR
    return None


def check_stop_distance(
    *,
    avg_entry: Decimal,
    stop: Decimal,
    atr: Decimal,
    min_multiple: Decimal,
    max_multiple: Decimal,
) -> RejectionReason | None:
    """Rules 3 and 4 — measured from the weighted average entry, as §2.5 and §4 do.

    Below 0.6 x ATR the stop sits inside the noise and guarantees a stop-out;
    above 3 x ATR it is a lazy stop that wrecks the RR.
    """
    distance = abs(avg_entry - stop)
    if distance < min_multiple * atr:
        return RejectionReason.STOP_TOO_TIGHT
    if distance > max_multiple * atr:
        return RejectionReason.STOP_TOO_WIDE
    return None


def rr_multiples(
    *, avg_entry: Decimal, stop: Decimal, targets: tuple[Decimal, ...]
) -> tuple[Decimal, ...]:
    """Reward-to-risk for each target, from the weighted average entry (§2.5)."""
    risk = abs(avg_entry - stop)
    return tuple(abs(target - avg_entry) / risk for target in targets)


def check_rr(*, rr: tuple[Decimal, ...], min_rr_tp1: Decimal) -> RejectionReason | None:
    """Rule 5 — only TP1 is gated; later targets are informational."""
    if rr[0] < min_rr_tp1:
        return RejectionReason.RR_TOO_LOW
    return None


def check_confidence(*, confidence: int, min_confidence: int) -> RejectionReason | None:
    """Rule 6 — below the bar the idea is downgraded to WATCHLIST, not rejected."""
    if confidence < min_confidence:
        return RejectionReason.LOW_CONFIDENCE
    return None
