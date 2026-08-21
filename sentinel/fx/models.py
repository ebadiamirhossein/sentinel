"""Forex value types (M10b, docs/specs/FOREX.md).

Deliberately **not** an extension of ``sentinel/risk/models.py``. That module is
frozen for the live measurement window, and more importantly it is crypto-shaped: a
``TradePlan`` carries ``notional_usdt``, ``suggested_leverage``, ``liq_distance_pct``
and ``liq_buffer_ok``, and ``PlanCosts`` carries a perpetual-futures funding rate.

§7.6 is explicit that CFD margin is account-level, that there is no per-position
liquidation price, and that the crypto liquidation-buffer rule "does not apply and
must not be faked". So the rule these types are built to (owner ruling, 2026-08-21)
is stronger than "leave it null": **if a concept does not exist in this market,
there is no field to put it in.** There is no liquidation field here and no funding
field here, absent rather than ``None`` and rather than zero, for the same reason
§2.1 refuses a zero volume — a meaningless value that looks meaningful is the exact
failure class this project keeps meeting.

Swap/rollover *is* a real forex concept (§2's ledger replaces funding with it), so it
is present and named ``rollover``, never ``funding``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ForexRejection(StrEnum):
    """Why a forex setup produced no signal. Its **own** closed vocabulary.

    Not ``sentinel.risk.models.RejectionReason``: that enum is frozen with the risk
    engine, and half of these have no crypto analogue while several of its codes have
    no forex meaning. Sharing one would make M9's "what is the gate rejecting, and was
    it right to" unanswerable for either market.
    """

    # ── the clock (§5) ──────────────────────────────────────────────────────
    #: Outside the trading week. A **normal state**, not a failure and not DEGRADED.
    MARKET_CLOSED = "MARKET_CLOSED"
    #: The first hours of the week, where D-d leaves the exact open ambiguous and
    #: liquidity is thin anyway. Nothing is lost by staying quiet.
    WEEK_OPEN_QUIET = "WEEK_OPEN_QUIET"
    #: Past the Friday cutoff: a ladder placed now cannot fill before the weekend,
    #: and a fill would carry gap risk through it.
    FRIDAY_CUTOFF = "FRIDAY_CUTOFF"
    #: The 19:00-21:00 UTC rollover window, used only when the measured spread series
    #: is unavailable — the spread trigger below is the primary rail.
    ROLLOVER_WINDOW = "ROLLOVER_WINDOW"

    # ── the spread (§5.3, §7.3) ─────────────────────────────────────────────
    #: The current spread exceeds a configured multiple of this instrument's
    #: **global** median. Global and not per-hour-of-day: see sentinel/fx/spread.py.
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"

    # ── the calendar (§8) ───────────────────────────────────────────────────
    #: Inside a currency-matched high-impact event window.
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    #: The hand-maintained calendar is missing or its coverage has run out. There is
    #: no second source, so this suppresses rather than degrades.
    CALENDAR_STALE = "CALENDAR_STALE"

    # ── sizing and cost (§7) ────────────────────────────────────────────────
    #: The position that carries the intended risk is below ``MinimumTradeSize``
    #: (1000 units on all three pairs), and rounding **up** would take more risk than
    #: the budget allows. How often this fires at EUR 200 is itself a measurement.
    BELOW_MIN_TICKET = "BELOW_MIN_TICKET"
    #: Reward-to-risk after spread, commission and rollover.
    NET_RR_TOO_LOW = "NET_RR_TOO_LOW"
    #: No capital set, so nothing can be sized.
    NO_CAPITAL = "NO_CAPITAL"
    #: No EUR->quote rate: sizing in EUR is impossible and is not guessed at.
    FX_RATE_UNAVAILABLE = "FX_RATE_UNAVAILABLE"

    # ── data (§4, §12) ──────────────────────────────────────────────────────
    #: The newest closed candle is too old **while the market is open** — which is
    #: the only time that means anything. See sentinel/fx/hours.py.
    STALE_CANDLES = "STALE_CANDLES"
    #: §9's hard cap of one open forex position for the first measurement window.
    MAX_CONCURRENT_POSITIONS = "MAX_CONCURRENT_POSITIONS"

    # ── M10c: the gate rows §16.5 added ─────────────────────────────────────
    #
    # Where the concept is genuinely the same as crypto's, the NAME is the same, so a
    # /pulse rejection histogram reads alike across two markets. Where it is not, the
    # name says so — see MARGIN_ABOVE_EQUITY_SHARE.

    # Preconditions (§16.5 row 1).
    #: The analyst did not put forward a candidate. Not a fault.
    NOT_A_CANDIDATE = "NOT_A_CANDIDATE"
    #: A CANDIDATE arrived without an entry zone, a stop or a target.
    MISSING_PLAN_FIELDS = "MISSING_PLAN_FIELDS"
    #: The Uic never resolved, so there is no pip and no minimum trade size. Skipped
    #: with a named reason, never silently (§4.2).
    INSTRUMENT_UNRESOLVED = "INSTRUMENT_UNRESOLVED"
    #: No ATR(1h), so the stop-distance bounds cannot be checked. Refused rather than
    #: checked against nothing: an unbounded stop is how a 40-pip idea becomes a
    #: 400-pip one with no symptom.
    ATR_UNAVAILABLE = "ATR_UNAVAILABLE"

    # Geometry and quality (§16.5 row 5).
    #: low >= high, or a zone the last price is nowhere near.
    ENTRY_ZONE_INVALID = "ENTRY_ZONE_INVALID"
    #: The stop is on the wrong side of the entry for the stated direction.
    STOP_SIDE = "STOP_SIDE"
    #: Targets are not ordered away from the entry.
    TARGET_ORDER = "TARGET_ORDER"
    #: An edge of the entry zone sits further from the last price than
    #: ``ForexConfig.max_entry_distance_pct``. **0.5%, not crypto's 3%** — spec
    #: defect #21: EURUSD moves about 0.5% in a day, so crypto's bound could never
    #: fire here and a rail that cannot fire reads on a checklist as a rail.
    ENTRY_TOO_FAR = "ENTRY_TOO_FAR"
    #: Stop closer to the entry than ``stop_atr_min_multiple`` x ATR(1h) — noise.
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    #: Stop further than ``stop_atr_max_multiple`` x ATR(1h).
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    #: Reward-to-risk **before** costs. Distinct from NET_RR_TOO_LOW on purpose, and
    #: for the reason crypto keeps them distinct: "the analyst proposed a poor RR" and
    #: "the setup was fine and the spread ate it" call for different actions.
    RR_TOO_LOW = "RR_TOO_LOW"
    #: Below ``min_confidence``. **Downgrades to watchlist**, never rejects.
    LOW_CONFIDENCE = "LOW_CONFIDENCE"

    # Rails (§16.5 row 6).
    #: A pause is active — global, per market, per user or per (user, market). The
    #: forex gate is handed the composed verdict, never the four rails.
    PAUSED = "PAUSED"
    #: This symbol resolved a signal recently and is still quiet.
    SYMBOL_COOLDOWN = "SYMBOL_COOLDOWN"
    #: ``ForexConfig.max_signals_per_day``. Three, not crypto's five: there are three
    #: instruments and they all cross the dollar.
    DAILY_SIGNAL_CAP = "DAILY_SIGNAL_CAP"

    # Margin (§16.5 row 8).
    #: Required margin exceeds ``max_margin_pct_of_equity``. Deliberately **not**
    #: crypto's ``MARGIN_BUDGET_EXCEEDED``: crypto budgets margin per position and
    #: forex margin is account-level (§7.6), and one name over two meanings is how
    #: that distinction gets forgotten.
    MARGIN_ABOVE_EQUITY_SHARE = "MARGIN_ABOVE_EQUITY_SHARE"


class ForexGateStatus(StrEnum):
    """The forex gate's verdict. Its own enum, with crypto's **wire values**.

    Not an import of :class:`sentinel.risk.models.GateStatus`, for the reason
    ``sentinel/fx/rounding.py`` is not an import of ``sentinel/risk/rounding.py``:
    this package depends on nothing in the frozen one, so new-market code is never
    coupled to a module nobody is allowed to touch.

    But the three **strings** are deliberately identical, because they are written to
    ``gate_decisions.status`` and read back by ``/pulse`` and ``/stats``, which group
    across markets. Two vocabularies that disagreed would split one histogram in half
    with nothing to say so. ``tests/fx/test_gate.py`` asserts the two still agree, so
    a divergence is a failing test rather than a quietly wrong count.
    """

    APPROVED_FOR_HUMAN = "APPROVED_FOR_HUMAN"
    REJECTED = "REJECTED"
    DOWNGRADED_WATCHLIST = "DOWNGRADED_WATCHLIST"


def fingerprint(secret: SecretStr | None) -> str:
    """A token's sha256 prefix, which is the only form one may ever be logged in.

    The spike compared two runs' credentials this way and never printed a value; this
    is the same discipline, made reusable. Sixteen hex characters is plenty to tell
    "the token rotated" from "it did not" and useless for anything else.
    """
    if secret is None:
        return "absent"
    digest = hashlib.sha256(secret.get_secret_value().encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


class SaxoTokenBundle(BaseModel):
    """The rotating Saxo credential, and everything read from the response (§3).

    Every lifetime here is **read**, never assumed. journal/M10b_SPIKE.md observed
    ``expires_in`` of 1070, 1182 and 1200 and ``refresh_token_expires_in`` of 3582 and
    3600 — not round numbers, and different between responses. So expiries are stored
    as absolute instants derived from the response's own figures at the moment it
    arrived, and there is no constant anywhere that a drift could invalidate.

    ``refresh_expires_at`` is **nullable and means "not known"**, not "does not
    expire". A bootstrap refresh token pasted in after a manual browser login carries
    no lifetime we were told; inventing a plausible hour for it would be the same
    class of mistake as inventing a pip.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_token: SecretStr
    obtained_at: datetime
    access_token: SecretStr | None = None
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    #: How many times the chain has rotated since it was seeded. A counter that stops
    #: climbing is the visible symptom of a persist that is failing silently.
    refresh_count: int = Field(default=0, ge=0)

    @property
    def refresh_token_value(self) -> str:
        return self.refresh_token.get_secret_value()

    @property
    def access_token_value(self) -> str | None:
        return None if self.access_token is None else self.access_token.get_secret_value()

    @property
    def refresh_fingerprint(self) -> str:
        return fingerprint(self.refresh_token)

    @property
    def access_fingerprint(self) -> str:
        return fingerprint(self.access_token)

    def access_is_usable(self, now: datetime, *, margin: timedelta) -> bool:
        """Is the access token good for a call made right now, with room to spare?

        ``margin`` is what stops a token dying mid-flight: a request that starts
        valid and arrives expired is indistinguishable at this end from a credential
        that was never any good.
        """
        if self.access_token is None or self.access_expires_at is None:
            return False
        return now + margin < self.access_expires_at

    def refresh_is_dead(self, now: datetime) -> bool:
        """Has the refresh chain lapsed? Unknown expiry is **not** dead (§3.1).

        A bootstrap token's lifetime is unknown, so the only honest way to find out
        whether it still works is to use it. Treating unknown as dead would refuse a
        credential that was pasted in thirty seconds ago.
        """
        if self.refresh_expires_at is None:
            return False
        return now >= self.refresh_expires_at


__all__ = ["ForexGateStatus", "ForexRejection", "SaxoTokenBundle", "fingerprint"]
