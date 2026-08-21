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


__all__ = ["ForexRejection", "SaxoTokenBundle", "fingerprint"]
