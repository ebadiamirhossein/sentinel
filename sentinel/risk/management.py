"""Management plan and time stop (§5) — a deterministic template, never LLM prose."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.analyst.models import TimeframeLabel
from sentinel.core.config import ManagementConfig


def management_plan_text(config: ManagementConfig) -> str:
    """§5's default text, with the percentages taken from config."""
    return (
        f"TP1: close {_num(config.tp1_close_pct)}%, move stop to breakeven. "
        f"TP2: close {_num(config.tp2_close_pct)}%. "
        f"TP3: close remainder or trail by {_num(config.tp3_trail_atr_multiple)}xATR."
    )


def expires_at(
    *, created_at: datetime, timeframe_label: TimeframeLabel, config: ManagementConfig
) -> datetime:
    """§5 time stop: the signal expires if no entry fills within the TTL.

    An unlabelled idea gets the shorter intraday TTL — the conservative default.
    """
    hours = (
        config.entry_ttl_hours_swing
        if timeframe_label is TimeframeLabel.SWING
        else config.entry_ttl_hours_intraday
    )
    return created_at + timedelta(hours=hours)


def _num(value: Decimal) -> str:
    """Render 40 as "40" and 1.0 as "1" without scientific notation."""
    return f"{value.normalize():f}"
