"""Risk-engine contracts (specs/RISK_ENGINE.md §1, §6, §7).

All money is ``Decimal``. Percentages that a human reads (``stop_distance_pct``,
``liq_distance_pct``, ``weight_pct``) are stored as **percent** — 1.78 means
1.78%. The internal math uses fractions and says so at each call site, and the
stored figure is the 2dp one the owner reads (see ``rounding.percent``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from sentinel.analyst.models import AnalystReport, Direction, SetupType, TimeframeLabel
from sentinel.ingestion.models import InstrumentMeta

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a features->risk import cycle
    from sentinel.features.models import SymbolFeatures
    from sentinel.ingestion.models import MarketSnapshot


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


class GateStatus(StrEnum):
    """ARCHITECTURE.md contract 4, plus §2 rule 6's downgrade — which is neither
    an approval nor a rejection."""

    APPROVED_FOR_HUMAN = "APPROVED_FOR_HUMAN"
    REJECTED = "REJECTED"
    DOWNGRADED_WATCHLIST = "DOWNGRADED_WATCHLIST"


class RejectionReason(StrEnum):
    """Machine-readable codes. Stored on every non-approved decision so M9 can ask
    "what is the gate rejecting most often, and was it right to?" — a prose
    message alone cannot be grouped or counted."""

    # Preconditions
    NOT_A_CANDIDATE = "NOT_A_CANDIDATE"
    MISSING_PLAN_FIELDS = "MISSING_PLAN_FIELDS"
    NO_CAPITAL = "NO_CAPITAL"
    ATR_UNAVAILABLE = "ATR_UNAVAILABLE"
    INSTRUMENT_META_MISSING = "INSTRUMENT_META_MISSING"
    FX_UNAVAILABLE = "FX_UNAVAILABLE"
    # §2 rules 1-6
    ENTRY_ZONE_INVALID = "ENTRY_ZONE_INVALID"
    STOP_SIDE = "STOP_SIDE"
    TARGET_ORDER = "TARGET_ORDER"
    ENTRY_TOO_FAR = "ENTRY_TOO_FAR"
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    RR_TOO_LOW = "RR_TOO_LOW"
    #: §2 rule 5 measured NET of costs (correction 2026-08-18). Deliberately a
    #: separate code from RR_TOO_LOW: "the analyst proposed a poor RR" and "the
    #: setup was fine and fees ate it" call for different prompt-tuning actions,
    #: and M9 cannot tell them apart if they share a code.
    NET_RR_TOO_LOW = "NET_RR_TOO_LOW"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    # §2 rule 7 / §7
    MAX_OPEN_RISK = "MAX_OPEN_RISK"
    MAX_POSITIONS = "MAX_POSITIONS"
    #: specs/TELEGRAM_UX.md §6's anti-spam cap. Deliberately its own code rather
    #: than folded into MAX_POSITIONS: "the account is full" and "the system has
    #: said enough for one day" are different findings, and M9 cannot separate
    #: them if they share a code.
    DAILY_SIGNAL_CAP = "DAILY_SIGNAL_CAP"
    SYMBOL_COOLDOWN = "SYMBOL_COOLDOWN"
    PAUSED = "PAUSED"
    # §4 sizing
    MIN_NOTIONAL = "MIN_NOTIONAL"
    LIQ_BUFFER = "LIQ_BUFFER"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    #: §4's ``margin_budget_pct`` as a hard limit (owner ruling 2026-08-19, M8.2).
    #: Deliberately its own code rather than folded into ``INSUFFICIENT_MARGIN``:
    #: "the owner cannot fund this at all" and "this exceeds the share of capital
    #: they allocated to one trade" are different findings with different fixes —
    #: the first needs a smaller setup, the second is a knob — and M9 cannot tell
    #: them apart if they share a code.
    MARGIN_BUDGET_EXCEEDED = "MARGIN_BUDGET_EXCEEDED"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class AccountState(Frozen):
    """Owner-set values (§1). ``capital_eur`` is None until ``/capital`` runs."""

    capital_eur: Decimal | None = None
    risk_per_trade_pct: Decimal = Decimal("0.75")
    #: USD per EUR, as Frankfurter publishes it (base=EUR) — EUR x rate = USDT.
    eurusd_rate: Decimal = Decimal("1")


class PauseReason(StrEnum):
    MANUAL = "MANUAL"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"


class PauseState(Frozen):
    """§7. A manual pause has no expiry; a loss-limit pause lapses after 24h."""

    paused: bool = False
    reason: PauseReason | None = None
    until: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        if not self.paused:
            return False
        return self.until is None or now < self.until


class PortfolioState(Frozen):
    """Everything the rails need, supplied by the caller — the engine reads no DB.

    M7's tracker fills this from Postgres on every cycle and every tick.
    """

    open_risk_pct: Decimal = Decimal("0")
    open_positions: int = 0
    #: symbol -> the instant its cooldown expires (ARCHITECTURE.md §3 dedup guard).
    cooldown_until: dict[str, datetime] = Field(default_factory=dict)
    pause: PauseState = PauseState()
    #: Realized loss today as a positive percentage of capital (§7).
    realized_loss_today_pct: Decimal = Decimal("0")
    #: Signals already published today, UTC (specs/TELEGRAM_UX.md §6's cap).
    signals_today: int = 0


class MarketContext(Frozen):
    """The market facts the gate needs: last price, ATR(14,1h), exchange rules and
    the current funding rate (§4.2 — the funding cost estimate's only input)."""

    symbol: str
    last_price: Decimal
    atr_1h: Decimal | None = None
    instrument: InstrumentMeta | None = None
    #: Per-settlement funding rate as a fraction (0.0000193 = 0.00193%). Positive
    #: means longs pay shorts. ``None`` when the snapshot has no derivatives data:
    #: the funding estimate then degrades explicitly rather than assuming zero.
    funding_rate: Decimal | None = None
    #: Anchor for counting settlements across the plan's expiry window.
    next_funding_time: datetime | None = None

    @classmethod
    def from_snapshot(
        cls, snapshot: MarketSnapshot, features: SymbolFeatures | None = None
    ) -> MarketContext:
        """Assemble from M1/M2 output. Missing ATR stays missing — never guessed,
        and the same goes for a missing funding rate."""
        atr: Decimal | None = None
        if features is not None:
            primary = features.timeframes.get("1h")
            atr = primary.atr14 if primary is not None else None
        deriv = snapshot.derivatives
        return cls(
            symbol=snapshot.symbol,
            last_price=snapshot.last_price,
            atr_1h=atr,
            instrument=snapshot.instrument,
            funding_rate=deriv.funding_rate if deriv is not None else None,
            next_funding_time=deriv.next_funding_time if deriv is not None else None,
        )


# --------------------------------------------------------------------------- #
# Ladder & plan
# --------------------------------------------------------------------------- #


class LadderRung(Frozen):
    """A price and its share of the **risk budget** (§3, owner ruling 2026-08-18)."""

    price: Decimal
    weight_pct: Decimal


class EntryRung(Frozen):
    """A sized rung: what the owner actually places as a limit order."""

    price: Decimal
    weight_pct: Decimal
    qty: Decimal
    notional_usdt: Decimal
    notional_eur: Decimal
    #: How far this rung sits from ``TradePlan.last_price``, **signed**: negative
    #: is below the current price. Added 2026-08-18 (owner directive, from M6) so
    #: the card can say "-0.3597%" without computing it. The sign is stored rather
    #: than inferred from ``direction`` because §2 rule 2 bounds both zone edges
    #: within 3% of price without forcing the zone to one side of it — a ladder
    #: can straddle the last price, and then the two ends have opposite signs.
    distance_pct: Decimal = Decimal("0")


class PlanCosts(Frozen):
    """§4.2 (correction 2026-08-18) — what this trade costs before it makes anything.

    Fees are certain; funding is an **estimate** and every field carrying it says
    so. Two funding numbers, deliberately:

    * ``funding_eur`` is the honest signed estimate — negative means the position
      is *paid* (a short while the rate is positive). This is what the card shows.
    * ``funding_charged_eur`` is what the gate uses. With
      ``costs.credit_favourable_funding`` false it is ``max(0, funding_eur)``, so a
      credit can never be the reason a plan clears ``min_rr_tp1``.
    """

    maker_fee_pct: Decimal
    taker_fee_pct: Decimal

    #: Maker fee on the full ladder — every rung's own notional at its own price.
    entry_fee_eur: Decimal
    #: Taker fee on a full stop-out. Part of net *risk*.
    stop_exit_fee_eur: Decimal
    #: Taker fee on a full exit at each target, in target order. Part of net *reward*.
    tp_exit_fees_eur: tuple[Decimal, ...]

    #: The snapshot's current per-settlement rate, as a fraction. None → unavailable.
    funding_rate: Decimal | None = None
    funding_interval_hours: int = 8
    #: Settlements falling inside (created_at, expires_at].
    funding_settlements: int = 0
    #: Signed estimate: positive = paid out, negative = received.
    funding_eur: Decimal = Decimal("0")
    #: What net RR actually charges — never negative unless credits are enabled.
    funding_charged_eur: Decimal = Decimal("0")
    #: False when the snapshot carried no funding rate. The estimate is then 0 and
    #: the card says so, rather than implying funding is free (CLAUDE.md: degrade
    #: explicitly, never fabricate).
    funding_available: bool = False

    #: Entry fee + taker exit at the stop + charged funding — the worst realistic
    #: round trip, which is the one worth seeing before taking the trade.
    round_trip_cost_eur: Decimal = Decimal("0")
    #: ``round_trip_cost_eur`` as a percentage of the planned risk budget.
    cost_pct_of_risk: Decimal = Decimal("0")


class RealizedCosts(Frozen):
    """What a signal's costs turned out to be, once the tracker knows the legs.

    ``PlanCosts`` prices the round trip the plan *intends*; this prices the one
    that *happened*. They differ whenever the ladder fills partially, which is the
    normal case — M5.1 §10 flagged that carrying the estimate forward into M9's
    statistics would measure a cost the owner never paid.

    Same conventions as §4.2 throughout: entries maker, every exit taker, funding
    signed with ``funding_charged_eur`` spending only ``max(0, funding)`` unless
    ``credit_favourable_funding`` says otherwise.
    """

    maker_fee_pct: Decimal
    taker_fee_pct: Decimal
    entry_fee_eur: Decimal
    exit_fee_eur: Decimal
    filled_notional_eur: Decimal
    funding_rate: Decimal | None = None
    funding_settlements: int = 0
    #: Signed: positive is paid, negative is received.
    funding_eur: Decimal = Decimal("0")
    funding_charged_eur: Decimal = Decimal("0")
    funding_available: bool = False
    total_eur: Decimal = Decimal("0")


class TradePlan(Frozen):
    """§6 — everything the signal card shows. The bot renders; it never computes.

    **schema_version 2** (2026-08-18) adds ``costs`` and ``rr_targets_net``.
    ``rr_targets`` keeps both its name and its meaning — reward-to-risk *gross* of
    costs — so plans stored by M4/M5 stay readable exactly as written.

    **schema_version 3** (2026-08-18, owner directive from M6) adds ``last_price``,
    ``target_distances_pct`` and ``EntryRung.distance_pct``. The card needs to say
    how far a target is from the entry and how far each rung is from the current
    price; a number the card needs and the plan lacks is a gap in the engine, not
    a line to drop from the card. Additive only — ``gate_decisions.plan`` is JSONB,
    so plans stored under 1 and 2 stay readable and no backfill is required.
    """

    schema_version: int = 3
    plan_id: UUID = Field(default_factory=uuid4)
    created_at: datetime

    symbol: str
    direction: Direction
    setup_type: SetupType
    timeframe_label: TimeframeLabel
    confidence: int
    #: The analyst's report travels with the plan (ARCHITECTURE.md contract 4).
    report: AnalystReport

    entries: tuple[EntryRung, ...]
    #: §3's E = Σ(price x weight) — the conservative basis for every gate check.
    avg_entry: Decimal
    #: What the owner actually averages if every rung fills (quantity-weighted).
    avg_fill_price: Decimal
    stop: Decimal
    targets: tuple[Decimal, ...]
    #: Reward-to-risk **gross** of costs — §2.5's figure, unchanged since M4.
    rr_targets: tuple[Decimal, ...]
    #: The same targets net of fees and estimated funding (§4.2). ``min_rr_tp1``
    #: gates on ``rr_targets_net[0]``, not on ``rr_targets[0]``.
    rr_targets_net: tuple[Decimal, ...]
    #: Each target's distance from ``avg_entry`` as an **unsigned** percentage,
    #: parallel to ``targets``. Unsigned because a short's reward is a falling
    #: price and a minus sign there reads as a loss; the card supplies the "+".
    #: Measured from ``avg_entry``, the same basis as ``stop_distance_pct`` and
    #: every RR figure, so all three reconcile by hand on the card.
    target_distances_pct: tuple[Decimal, ...] = ()
    costs: PlanCosts

    stop_distance_pct: Decimal
    #: The market price the plan was built against — the reference every
    #: ``EntryRung.distance_pct`` is measured from. Stored so those percentages
    #: stay re-derivable by hand (PRD G5: a figure whose reference was discarded
    #: is not auditable).
    last_price: Decimal = Decimal("0")
    planned_risk_eur: Decimal
    #: Risk after quantities are floored to the exchange step — the real number.
    risk_eur: Decimal
    notional_usdt: Decimal
    notional_eur: Decimal
    margin_eur: Decimal
    suggested_leverage: int
    liq_distance_pct: Decimal
    liq_buffer_ok: bool

    management_plan: str
    expires_at: datetime

    # Inputs frozen into the plan: §7 requires open signals to keep their original
    # sizing even after /capital or /risk change.
    capital_eur: Decimal
    risk_per_trade_pct: Decimal
    eurusd_rate: Decimal
    instrument: InstrumentMeta

    gate_status: GateStatus = GateStatus.APPROVED_FOR_HUMAN


class GateDecision(Frozen):
    """The gate's verdict — always with a code, never prose alone (PRD G5)."""

    symbol: str
    status: GateStatus
    reason: RejectionReason | None = None
    message: str = ""
    plan: TradePlan | None = None
    evaluated_at: datetime
    prompt_version: str | None = None
