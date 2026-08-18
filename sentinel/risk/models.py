"""Risk-engine contracts (specs/RISK_ENGINE.md §1, §6, §7).

All money is ``Decimal``. Percentages that a human reads (``stop_distance_pct``,
``liq_distance_pct``, ``weight_pct``) are stored as **percent** — 1.7841 means
1.7841%. The internal math uses fractions and says so at each call site.
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
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    # §2 rule 7 / §7
    MAX_OPEN_RISK = "MAX_OPEN_RISK"
    MAX_POSITIONS = "MAX_POSITIONS"
    SYMBOL_COOLDOWN = "SYMBOL_COOLDOWN"
    PAUSED = "PAUSED"
    # §4 sizing
    MIN_NOTIONAL = "MIN_NOTIONAL"
    LIQ_BUFFER = "LIQ_BUFFER"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"


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


class MarketContext(Frozen):
    """The market facts the gate needs: last price, ATR(14,1h) and exchange rules."""

    symbol: str
    last_price: Decimal
    atr_1h: Decimal | None = None
    instrument: InstrumentMeta | None = None

    @classmethod
    def from_snapshot(
        cls, snapshot: MarketSnapshot, features: SymbolFeatures | None = None
    ) -> MarketContext:
        """Assemble from M1/M2 output. Missing ATR stays missing — never guessed."""
        atr: Decimal | None = None
        if features is not None:
            primary = features.timeframes.get("1h")
            atr = primary.atr14 if primary is not None else None
        return cls(
            symbol=snapshot.symbol,
            last_price=snapshot.last_price,
            atr_1h=atr,
            instrument=snapshot.instrument,
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


class TradePlan(Frozen):
    """§6 — everything the signal card shows. The bot renders; it never computes."""

    schema_version: int = 1
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
    rr_targets: tuple[Decimal, ...]

    stop_distance_pct: Decimal
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
