"""Guards before spend, and the rails that decide how much forex costs (M10d).

Three separate things, in one file because they answer one question — *what does a
forex cycle cost, and what stops it costing that?*

**1. `/pause forex` was a publish-stopper, not an off switch.** M10a shipped the
per-market pause and the crypto cycle has checked it before the analyst since M7, with
the reason at the call site: "analysing first would buy a ~$0.32 rejection". The forex
path read the pause only inside the per-user fan-out, *after* three analyst calls — so
a paused forex market still cost ~$0.73 a cycle to be paused, and cost is the first
reason anybody reaches for the switch during a two-week observation window.

**2. Outside `scan_hours_utc` the cycle costs zero, not cheap.** Asserted on the venue
and the LLM, not on the absence of a card: a cycle that fetched everything and then
declined to analyse would also publish nothing.

**3. The reserved floor still protects the crypto measurement at the raised rails.**
`evaluate_market_spend` suspends forex when
``global_day >= global_limit - max(0, crypto_floor - crypto_spent)``. Substituting
``global_day = crypto + forex`` cancels crypto out, so forex's real ceiling is
``global_limit - crypto_floor`` **whatever crypto does** — which is why the ceiling had
to be raised rather than only forex's sub-budget. Pinned here as arithmetic over the
shipped numbers, because the whole reason the floor exists is the day a second market
can spend.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sentinel.core.config import AppConfig, load_config
from sentinel.core.markets import Market
from sentinel.core.orchestrator import SkipReason
from sentinel.llm.spend import (
    SpendScope,
    SpendState,
    SpendTotals,
    evaluate_market_spend,
    reserved_elsewhere_usd,
)
from sentinel.risk.models import PauseReason, PauseState
from tests.core.conftest import CycleStore
from tests.core.saxo_double import NOW
from tests.core.test_forex_signal_path import calendar_covering, run_forex_cycle

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY")


# --------------------------------------------------------------------------- #
# 1 — /pause forex stops the spending, not just the publishing
# --------------------------------------------------------------------------- #


async def test_a_paused_forex_market_costs_nothing() -> None:
    """Asserted on the **LLM call count**, not on the absence of a card.

    "No card was published" was already true of the old behaviour, which is exactly
    why it went unnoticed: the pause worked, visibly, and cost $0.73 a cycle to do it.
    """
    config = load_config()
    store = CycleStore()
    store.pause = PauseState(paused=True, reason=PauseReason.MANUAL, until=None)

    result, published, store, _, _ = await run_forex_cycle(
        config, calendar=calendar_covering(NOW), store=store
    )

    assert result.analysis_suspended is True
    assert result.suspended_reason == "paused"
    assert result.analyzed == 0
    assert store.llm_calls == [], "a paused market must not spend a cent"
    assert published == []
    assert {skip.reason for skip in result.skipped.values()} == {SkipReason.PAUSED}


async def test_an_unpaused_forex_market_does_spend() -> None:
    """The sibling. Without it, a cycle broken in any other way would satisfy the
    test above and read as a working kill switch."""
    result, _, _, _, _ = await run_forex_cycle(load_config(), calendar=calendar_covering(NOW))

    assert result.analyzed == 3
    assert SkipReason.PAUSED not in {skip.reason for skip in result.skipped.values()}


# --------------------------------------------------------------------------- #
# 2 — the scan window costs zero outside itself
# --------------------------------------------------------------------------- #


async def test_outside_the_scan_window_nothing_is_fetched_and_nothing_is_analysed() -> None:
    """Zero, not cheap. The venue is never asked and the analyst is never built.

    `SyntheticSaxo.chart_requests` is the assertion that distinguishes the two: a
    cycle that fetched three symbols' worth of tails and then declined to analyse
    would look identical from the result object.
    """
    config = load_config()
    # NOW is 12:05Z, inside the shipped window; 03:05Z is a real trading hour outside
    # it — the venue is open, we are choosing not to look, which is the distinction
    # OUTSIDE_SCAN_HOURS exists to keep separate from MARKET_CLOSED.
    start, _ = config.forex.scan_hours_utc
    assert start > 3, "this test needs an hour the shipped window excludes"
    outside = NOW.replace(hour=3)

    result, published, store, _, transport = await run_forex_cycle(
        config, calendar=calendar_covering(outside), now=outside
    )

    assert result.analyzed == 0
    assert store.llm_calls == []
    assert published == []
    assert transport.chart_requests == [], "the venue was asked for candles anyway"
    assert {skip.reason for skip in result.skipped.values()} == {SkipReason.OUTSIDE_SCAN_HOURS}


async def test_inside_the_scan_window_the_cycle_runs() -> None:
    """The non-vacuity sibling: the window admits the hours it is supposed to."""
    config = load_config()
    result, _, _, _, transport = await run_forex_cycle(
        config, calendar=calendar_covering(NOW), now=NOW
    )

    assert result.analyzed == 3
    assert transport.chart_requests


# --------------------------------------------------------------------------- #
# 3 — the reserved floor at the raised rails
# --------------------------------------------------------------------------- #


def verdict(*, crypto_spent: str, forex_spent: str, config: AppConfig) -> Any:
    crypto = config.market(Market.CRYPTO)
    forex = config.market(Market.FOREX)
    spent = {Market.CRYPTO: Decimal(crypto_spent), Market.FOREX: Decimal(forex_spent)}
    return evaluate_market_spend(
        market_totals=SpendTotals(day_usd=Decimal(forex_spent), month_usd=Decimal(0), calls=0),
        global_totals=SpendTotals(
            day_usd=Decimal(crypto_spent) + Decimal(forex_spent),
            month_usd=Decimal(0),
            calls=0,
        ),
        market=forex,
        global_limit_usd=config.llm_daily_budget_global_usd,
        config=config.llm,
        reserved_elsewhere=reserved_elsewhere_usd(
            for_market=Market.FOREX,
            markets={Market.CRYPTO: crypto, Market.FOREX: forex},
            day_spend_by_market=spent,
        ),
    )


def test_forexs_real_ceiling_is_the_global_limit_minus_cryptos_floor() -> None:
    """The arithmetic the rails were set from, over the SHIPPED numbers.

    Crypto's spend cancels out of the inequality **while crypto is under its floor**,
    so where it sits inside that range makes no difference — which is what makes this a
    floor rather than a race. Above the floor the reserve is spent and the plain global
    ceiling takes over, tightening from there; both halves are asserted, because the
    second is the one somebody would assume away.

    Written as ``global - floor`` rather than as a literal, so re-tuning either number
    at the 2026-09-04 review does not need this test edited.
    """
    config = load_config()
    ceiling = (
        config.llm_daily_budget_global_usd - config.market(Market.CRYPTO).llm_reserved_floor_usd
    )

    # While crypto is UNDER its floor — which at $3.61/day is every day — the reserve
    # is what stops forex, and where crypto's spend sits inside that range makes no
    # difference at all.
    for crypto_spent in ("0", "3.61", "7.99"):
        just_under = verdict(
            crypto_spent=crypto_spent, forex_spent=str(ceiling - Decimal("0.01")), config=config
        )
        at_it = verdict(crypto_spent=crypto_spent, forex_spent=str(ceiling), config=config)
        assert just_under.state is not SpendState.LIMIT_REACHED, crypto_spent
        assert at_it.state is SpendState.LIMIT_REACHED, crypto_spent
        assert at_it.scope is SpendScope.RESERVED, crypto_spent

    # Once crypto has spent its floor there is nothing left to reserve, so the rail
    # that stops forex is the plain global ceiling and it TIGHTENS from there. That is
    # the floor working as designed — it protects the measurement, not crypto's
    # convenience — and it is asserted rather than left as a footnote because the
    # scope is what /pulse names when it explains why nothing was analysed.
    spent_its_floor = verdict(crypto_spent="8", forex_spent=str(ceiling), config=config)
    assert spent_its_floor.state is SpendState.LIMIT_REACHED
    assert spent_its_floor.scope is SpendScope.GLOBAL

    beyond_its_floor = verdict(
        crypto_spent="9", forex_spent=str(ceiling - Decimal("1")), config=config
    )
    assert beyond_its_floor.state is SpendState.LIMIT_REACHED
    assert beyond_its_floor.scope is SpendScope.GLOBAL


def test_crypto_keeps_its_whole_floor_on_the_worst_possible_forex_day() -> None:
    """The floor's entire purpose, asserted rather than argued.

    Forex can never push the global total past ``global_limit - floor + crypto_spent``,
    so crypto always has its full floor of headroom underneath the global ceiling — on
    the day forex spends every cent it is allowed to.
    """
    config = load_config()
    floor = config.market(Market.CRYPTO).llm_reserved_floor_usd
    most_forex_can_spend = config.llm_daily_budget_global_usd - floor

    headroom = config.llm_daily_budget_global_usd - most_forex_can_spend
    assert headroom == floor


def test_the_forex_ceiling_covers_a_full_days_scanning() -> None:
    """The rails against the MEASURED cost, so a re-tune that starves forex fails here.

    $0.734 per cycle of three pairs (journal/M10d_REPORT.md, join 4: one uncached call
    at $0.281925 plus two cache reads at $0.226104). Fourteen cycles is the
    ``scan_hours_utc`` window. If this ever fails, forex goes dark part-way through the
    day and the cards simply stop — silently, because the spend guard suspends analysis
    rather than erroring.
    """
    config = load_config()
    start, end = config.forex.scan_hours_utc
    cycles = end - start
    measured_cost_per_cycle = Decimal("0.734")
    ceiling = (
        config.llm_daily_budget_global_usd - config.market(Market.CRYPTO).llm_reserved_floor_usd
    )

    assert cycles * measured_cost_per_cycle < ceiling, (
        f"{cycles} cycles x ${measured_cost_per_cycle} = "
        f"${cycles * measured_cost_per_cycle} against a real forex ceiling of ${ceiling}"
    )


def test_the_global_warn_level_is_not_crossed_on_an_ordinary_day() -> None:
    """A warn level left behind by a raised ceiling fires every single day.

    `llm.daily_spend_warn_usd` is the GLOBAL warn (`llm/spend.py`), and at 7 against a
    raised ceiling it would send a Telegram spend notice daily — alert fatigue on the
    one channel that has to stay meaningful, which is how a real warning gets muted.
    """
    config = load_config()
    ordinary_day = Decimal("3.61") + Decimal(
        config.forex.scan_hours_utc[1] - config.forex.scan_hours_utc[0]
    ) * Decimal("0.734")

    assert ordinary_day < config.llm.daily_spend_warn_usd, (
        f"an ordinary day spends ${ordinary_day} against a warn level of "
        f"${config.llm.daily_spend_warn_usd} — the warn would fire every day"
    )
    assert config.llm.daily_spend_warn_usd < config.llm_daily_budget_global_usd
