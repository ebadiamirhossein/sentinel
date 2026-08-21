"""When a forex ladder dies, and why (FOREX.md §5.4, §16.10).

A crypto ladder has one way to expire: its TTL runs out. A forex ladder has two, and
the second has no 24/7 analogue at all — **the week ends**. A ladder resting over a
weekend cannot fill, and a rung that filled on the Sunday open would fill against a gap
nobody's stop was placed for. §5.4 is explicit that such a ladder is **expired, with a
reason the owner sees** — not paused, not silently carried.

So expiry is ``min(created_at + ttl, this week's Friday ladder expiry)`` and the plan
records **which of the two it was** in ``expiry_basis``. One timestamp cannot say, and
"expired after 12 hours" and "expired because the week ended four hours early" are
different facts about the same signal — the first is the market not coming to the level,
the second is the calendar.

``friday_ladder_expiry_hour_utc`` is a config hour rather than a derived one because the
real Friday close moves by an hour twice a year (§5.2), and config load refuses any
value at or after ``week_close_hour_utc``: at or after it the ladder simply survives the
weekend and the rail silently stops existing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sentinel.analyst.models import TimeframeLabel
from sentinel.core.config import ForexConfig, ManagementConfig

FRIDAY = 4

#: What ``ForexPlan.expiry_basis`` says, so no surface has to phrase it itself.
TTL_BASIS = "the entry TTL"
WEEK_BASIS = "the Friday close, which arrives first"


def friday_ladder_expiry(now: datetime, config: ForexConfig) -> datetime:
    """The next instant at which every pending ladder expires (§5.4).

    "Next" from ``now``, so a plan created at 19:30 on a Friday — before the
    ``friday_signal_cutoff_hour_utc`` would have stopped it, in a config where it does
    not — gets *this* Friday's expiry rather than next week's. A ladder whose deadline
    has already passed is not a ladder.
    """
    days_ahead = (FRIDAY - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=config.friday_ladder_expiry_hour_utc, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def ttl_hours(timeframe_label: TimeframeLabel, management: ManagementConfig) -> int:
    """An unlabelled idea gets the shorter intraday TTL — the conservative default."""
    if timeframe_label is TimeframeLabel.SWING:
        return management.entry_ttl_hours_swing
    return management.entry_ttl_hours_intraday


def plan_expiry(
    *,
    created_at: datetime,
    timeframe_label: TimeframeLabel,
    management: ManagementConfig,
    forex: ForexConfig,
) -> tuple[datetime, str]:
    """``(expires_at, expiry_basis)`` — the earlier of the TTL and the Friday close."""
    by_ttl = created_at + timedelta(hours=ttl_hours(timeframe_label, management))
    by_week = friday_ladder_expiry(created_at, forex)
    if by_week < by_ttl:
        return by_week, WEEK_BASIS
    return by_ttl, TTL_BASIS


def carries_weekend_gap_risk(
    *,
    created_at: datetime,
    timeframe_label: TimeframeLabel,
    management: ManagementConfig,
    forex: ForexConfig,
) -> bool:
    """Could a position from this plan still be open when the week closes? (§5.4)

    Asked of the **holding period**, not of the ladder: the ladder expires before the
    close by construction, and the risk this flags belongs to a position that filled and
    is still open. A swing idea placed on a Wednesday reaches Friday evening; an
    intraday one placed on a Monday morning does not, and telling it a gap is coming
    would be a warning that cries wolf until nobody reads it.
    """
    horizon = created_at + timedelta(hours=ttl_hours(timeframe_label, management))
    return horizon >= friday_ladder_expiry(created_at, forex)


__all__ = [
    "TTL_BASIS",
    "WEEK_BASIS",
    "carries_weekend_gap_risk",
    "friday_ladder_expiry",
    "plan_expiry",
    "ttl_hours",
]
