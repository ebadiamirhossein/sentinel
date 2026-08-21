"""The forex trade plan (docs/specs/FOREX.md §16).

**Why this exists at all.** Spec defect #12: ``sentinel.risk.models.TradePlan`` lives in
the frozen package and is crypto-shaped. It carries ``notional_usdt``,
``suggested_leverage``, ``liq_distance_pct``, ``liq_buffer_ok`` and an
``InstrumentMeta``, and its ``PlanCosts`` carries six ``funding_*`` fields. Filling any
of those for forex means either a lie — ``liq_buffer_ok=True`` on a market with no
per-position liquidation price — or a zero standing in for a measurement. §7.6 forbids
the first outright and §2.1 forbids the second, and ``sentinel/risk/`` may not be edited.

Subclassing inherits the forbidden fields; a shared base class requires editing
``TradePlan``. So this is a **parallel, independently-defined model**, held to the same
rule :class:`~sentinel.fx.sizing.ForexSizing` and :class:`~sentinel.fx.costs.ForexCosts`
are already held to: *if a concept does not exist in this market, there is no field to
put it in.* ``tests/fx/test_plan.py`` asserts that rather than leaving it to habit.

**The mirroring is load-bearing, not cosmetic.** Every name this shares with
``TradePlan`` is shared on purpose: ``storage.repositories.signal_row()``,
``bot/publisher.py``, ``tracker/loop.py`` and every ``/positions``, ``/journal`` and
``/stats`` reader address a plan by these names, and matching them is what lets one
signals table, one publisher and one tracker serve both markets with no translation
layer in between.

**And the mirroring is a hazard, so §16.7 is the guard.** Two models that look alike are
two models a mis-dispatched rehydration could confuse — the same shape as the pip
derivation, where the wrong reading gives plausible numbers and no error. Rehydration
dispatches on ``row.market``, never on trying one model and falling back to the other,
and ``tests/bot/test_plan_dispatch.py`` proves both directions fail loudly and that
neither model's field set is a subset of the other's.

This module imports nothing from ``sentinel/risk/``. That is the ``fx/rounding.py``
precedent and it is deliberate: new-market code is never coupled to a module nobody is
allowed to touch.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from sentinel.analyst.models import AnalystReport, Direction, SetupType, TimeframeLabel
from sentinel.fx.costs import ForexCosts
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.models import ForexGateStatus, ForexRejection


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ForexEntryRung(Frozen):
    """A sized rung: what the owner actually places as a limit order.

    Mirrors :class:`sentinel.risk.models.EntryRung`, including the name **``qty``**.

    §16.2 as first written called this ``units``, on the argument that a forex position
    is a quantity of the base currency and that calling it a quantity of nothing in
    particular is how a units figure ends up where a money figure is read. **Reversed
    during the build, and the reversal is the more interesting half.** The danger that
    argument is about is real, and it is about the *notional* fields — a units figure in
    a money slot. ``qty`` is not a money slot. What ``units`` would actually have bought
    is a rename at every boundary that already speaks ``qty``: ``signal_fills.qty``,
    ``risk.accounting.Fill``, ``SignalTracking.filled_qty``, ``PositionView.filled_pct``
    and the tracker's whole detection path. That is precisely the translation layer
    §16.2 says the mirroring exists to avoid, bought with nothing.

    So: ``qty``, in base-currency units, and the unit is stated in the field comment and
    printed on the card next to the figure — which is where a reader needs it, rather
    than in a field name only the code sees.

    There is no ``notional_usdt``: this market is not quoted in USDT.
    """

    price: Decimal
    #: This rung's share of the **risk budget**, not of notional (§3's owner ruling).
    weight_pct: Decimal
    #: Quantity in **base-currency units** — 26,000 means 26,000 EUR of EURUSD. 1000 is
    #: the venue minimum on all three pairs (§7.2).
    qty: Decimal
    notional_eur: Decimal
    #: How far this rung sits from ``ForexPlan.last_price``, **signed** — negative is
    #: below. Stored rather than inferred from ``direction`` because a ladder may
    #: straddle the last price, and then the two ends have opposite signs.
    distance_pct: Decimal = Decimal("0")
    #: The same distance in **pips**, unsigned. Forex is discussed in pips and a
    #: four-decimal percentage is unreadable at 3am on a phone.
    distance_pips: Decimal = Decimal("0")


class ForexPlan(Frozen):
    """§16.2 — everything a forex signal card shows, and nothing it must not.

    Three groups, and which group a field is in is the whole design:

    * **mirrored from ``TradePlan``** — same spelling, same meaning, so the storage,
      publishing and tracking layers need no translation;
    * **forex-only** — ``pip``, ``pip_value_eur``, ``stop_distance_pips``,
      ``quote_currency``, ``notional_quote``, ``eur_quote_rate``, ``max_leverage`` with
      ``leverage_basis``, ``margin_pct_of_equity``, ``expiry_basis``,
      ``weekend_gap_warning``;
    * **absent** — no ``notional_usdt``, no ``suggested_leverage``, no
      ``liq_distance_pct``, no ``liq_buffer_ok``, no ``eurusd_rate``, nothing
      ``funding_*``. Absent, not ``None`` and not zero.

    Three of the forex-only fields need their reason stated here rather than in a spec
    somebody may not have open:

    ``eur_quote_rate``, **not** ``eurusd_rate``, because §7.1 sizes USDJPY through
    **EURJPY**. A field named ``eurusd_rate`` holding an EURJPY figure is a fabricated
    label on a correct number, and the ~145x error it invites sizes a position to
    roughly nothing while looking like an unremarkable rejection.

    ``max_leverage`` with ``leverage_basis``, **not** ``suggested_leverage``, because
    crypto *derives* its leverage from the liquidation buffer and forex has no
    per-position liquidation price to derive one from (§7.6). 30:1 is a configured cap
    resting on an ESMA assumption, and the words travel with the number so that no
    renderer can show one without the other. Two different concepts must not share a
    field name.

    ``expiry_basis`` in words, because §5.4 gives a ladder two different reasons to die
    — its own TTL, or the Friday close arriving first — and a bare timestamp cannot say
    which one it was.
    """

    #: 1, and it starts at 1 rather than continuing ``TradePlan``'s 3. This is a
    #: different contract with its own history; sharing a counter would imply the two
    #: evolve together, which is the confusion §16.7 exists to prevent.
    schema_version: int = 1
    plan_id: UUID = Field(default_factory=uuid4)
    created_at: datetime

    # ── mirrored: identity and the analyst's judgement ───────────────────────
    symbol: str
    direction: Direction
    setup_type: SetupType
    timeframe_label: TimeframeLabel
    confidence: int
    #: The analyst's report travels with the plan (ARCHITECTURE.md contract 4).
    report: AnalystReport

    # ── mirrored: the ladder and the levels ──────────────────────────────────
    entries: tuple[ForexEntryRung, ...]
    #: Sigma(price x weight) — the conservative basis every gate check uses.
    avg_entry: Decimal
    #: What the owner actually averages if every rung fills (quantity-weighted).
    avg_fill_price: Decimal
    stop: Decimal
    targets: tuple[Decimal, ...]
    #: Reward-to-risk **gross** of costs.
    rr_targets: tuple[Decimal, ...]
    #: The same targets net of the measured spread, commission and rollover (§7.4 as
    #: corrected by defect #16 — the spread lands on both sides). ``min_rr_tp1`` gates
    #: on ``rr_targets_net[0]``, never on ``rr_targets[0]``.
    rr_targets_net: tuple[Decimal, ...]
    #: Each target's distance from ``avg_entry``, **unsigned** — a short's reward is a
    #: falling price and a minus sign in front of it reads as a loss.
    target_distances_pct: tuple[Decimal, ...] = ()
    #: The same distances in pips, parallel to ``targets``.
    target_distances_pips: tuple[Decimal, ...] = ()
    costs: ForexCosts

    stop_distance_pct: Decimal
    #: The stop in pips. The unit this market is actually discussed in.
    stop_distance_pips: Decimal
    #: The market price the plan was built against — the reference every rung's
    #: ``distance_pct`` is measured from, stored so those stay re-derivable by hand.
    last_price: Decimal = Decimal("0")

    # ── mirrored: the money ──────────────────────────────────────────────────
    planned_risk_eur: Decimal
    #: Risk after units are floored to the venue's amount precision — the real number.
    #: Never greater than ``planned_risk_eur``: under-risking is fine, over-risking is
    #: not (§7.2).
    risk_eur: Decimal
    notional_eur: Decimal
    margin_eur: Decimal

    # ── forex-only ───────────────────────────────────────────────────────────
    quote_currency: str
    #: The position in quote-currency units — USD for the majors, JPY for the cross.
    notional_quote: Decimal
    pip: Decimal
    #: EUR gained or lost per pip by the **whole** position.
    pip_value_eur: Decimal
    #: How many quote-currency units one euro bought when this was sized. EURUSD for
    #: EURUSD and GBPUSD; **EURJPY** for USDJPY.
    eur_quote_rate: Decimal
    #: A configured cap, not a derived figure — see the class docstring.
    max_leverage: int
    #: Why ``max_leverage`` is what it is, in words, travelling with the number.
    leverage_basis: str
    #: Required margin as a percentage of capital. §7.6's rail is account-level, so
    #: this is a share of equity rather than a per-position budget.
    margin_pct_of_equity: Decimal
    #: True when this plan's holding period can reach the weekend break (§5.4). A
    #: position carried through it can open beyond its stop, and the card says so.
    weekend_gap_warning: bool = False

    # ── mirrored: management and expiry ──────────────────────────────────────
    management_plan: str
    expires_at: datetime
    #: Which rule set ``expires_at``, in words: the TTL, or the Friday close arriving
    #: first. §5.4 gives a pending ladder two ways to die and one timestamp.
    expiry_basis: str = ""

    # ── mirrored: inputs frozen into the plan (§7) ───────────────────────────
    capital_eur: Decimal
    risk_per_trade_pct: Decimal
    instrument: ForexInstrument

    gate_status: ForexGateStatus = ForexGateStatus.APPROVED_FOR_HUMAN


class ForexGateDecision(Frozen):
    """The forex gate's verdict — always with a code, never prose alone (PRD G5)."""

    symbol: str
    status: ForexGateStatus
    reason: ForexRejection | None = None
    message: str = ""
    plan: ForexPlan | None = None
    evaluated_at: datetime
    prompt_version: str | None = None

    @property
    def approved(self) -> bool:
        return self.plan is not None


__all__ = ["ForexEntryRung", "ForexGateDecision", "ForexPlan"]
