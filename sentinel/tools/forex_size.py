"""M10c demo: a fixture through the **forex** gate, printed as a plan and a card.

    python -m sentinel.tools.forex_size --fixture examples/eurusd_long.json
    python -m sentinel.tools.forex_size --fixture examples/eurusd_long.json --json
    python -m sentinel.tools.forex_size --fixture examples/eurusd_long.json --capital 200

No network, no LLM, no database: a fixture in, a decision out. The clock is frozen, so
the same fixture always produces the same plan and the same card.

``--capital 200`` is the one worth running. journal/M10b_REPORT.md §6 measured that at
the owner's actual capital the widest EURUSD stop that sizes at all is 17.5 pips, and
this fixture's stop is 35 — so it prints ``BELOW_MIN_TICKET`` and the ideal size that
was refused. That rejection is the measurement §7.2 asked for, not a broken demo.

The mirror of ``sentinel/tools/size.py``, deliberately: two markets, two gates, one way
of looking at either of them.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sentinel.analyst.models import AnalystReport
from sentinel.bot.forex_cards import forex_signal_card
from sentinel.bot.formatting import money_eur, percent_2dp
from sentinel.bot.models import SignalRecord
from sentinel.core.clock import FrozenClock
from sentinel.core.config import load_settings
from sentinel.core.logging import configure_logging
from sentinel.core.markets import Market
from sentinel.fx.calendar import EconomicCalendar
from sentinel.fx.gate import ForexAccountState, ForexGate, ForexMarketContext
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.plan import ForexGateDecision
from sentinel.fx.rails import ForexPortfolioState
from sentinel.fx.spread import SpreadProfile

#: A Wednesday, mid-London: clear of the rollover window, the Friday cutoff and the
#: quiet hours after the week opens, so no clock rail is silently doing the work.
DEMO_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def load_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def build_inputs(
    payload: dict[str, Any], *, capital_override: str | None
) -> tuple[AnalystReport, ForexMarketContext, ForexAccountState]:
    instrument = ForexInstrument.model_validate(payload["instrument"])
    market_payload = payload["market"]
    account_payload = dict(payload["account"])
    if capital_override is not None:
        account_payload["capital_eur"] = capital_override

    median = Decimal(market_payload["spread_median_pips"])
    profile = SpreadProfile(
        symbol=instrument.symbol,
        samples=int(market_payload["spread_samples"]),
        global_median_pips=median,
        median_by_hour_pips=dict.fromkeys(range(24), median),
        min_pips=median,
        max_pips=median,
    )
    market = ForexMarketContext(
        symbol=instrument.symbol,
        last_price=Decimal(market_payload["last_price"]),
        atr_1h=Decimal(market_payload["atr_1h"]),
        instrument=instrument,
        spread_profile=profile,
        current_spread_pips=Decimal(market_payload["current_spread_pips"]),
    )
    account = ForexAccountState(
        capital_eur=Decimal(account_payload["capital_eur"]),
        risk_per_trade_pct=Decimal(account_payload["risk_per_trade_pct"]),
        eur_quote_rate=Decimal(account_payload["eur_quote_rate"]),
    )
    return AnalystReport.model_validate(payload["report"]), market, account


def evaluate(path: Path, *, capital_override: str | None) -> ForexGateDecision:
    settings = load_settings()
    report, market, account = build_inputs(load_fixture(path), capital_override=capital_override)
    # A calendar with coverage and no events. The **shipped** one claims no coverage at
    # all (§8), so running this against it would print CALENDAR_STALE every time and
    # demonstrate the rail rather than the gate. That rail has its own tests.
    calendar = EconomicCalendar(
        events=(), coverage_until=DEMO_NOW.date().replace(year=2027), source="demo"
    )
    return ForexGate(settings.config, calendar=calendar, clock=FrozenClock(DEMO_NOW)).evaluate(
        report=report, market=market, account=account, portfolio=ForexPortfolioState()
    )


def describe(decision: ForexGateDecision) -> str:
    plan = decision.plan
    if plan is None:
        return f"REJECTED  {decision.symbol}  {decision.reason}\n  {decision.message}"

    lines = [
        f"{decision.status.value}  {plan.symbol}  {plan.direction.value}",
        "",
        f"  capital        €{money_eur(plan.capital_eur)}  ·  "
        f"risk {percent_2dp(plan.risk_per_trade_pct)}%"
        f"  ·  EUR{plan.quote_currency} {plan.eur_quote_rate}",
        f"  planned risk   €{plan.planned_risk_eur}   actual €{plan.risk_eur}",
        "",
        f"  ladder ({len(plan.entries)} rung(s))",
    ]
    for index, rung in enumerate(plan.entries, start=1):
        lines.append(
            f"    {index}) {rung.price}  {rung.weight_pct}% of risk  "
            f"{rung.qty} {plan.instrument.base_currency}  €{rung.notional_eur}"
        )
    lines += [
        f"  weighted entry {plan.avg_entry}   avg fill {plan.avg_fill_price}",
        f"  stop           {plan.stop}  ({plan.stop_distance_pips} pips, "
        f"-{plan.stop_distance_pct}%)",
        f"  pip value      €{plan.pip_value_eur} per pip  ·  1 pip = {plan.pip}",
        "",
        f"  notional       €{plan.notional_eur}  ({plan.notional_quote} {plan.quote_currency})",
        f"  margin         €{plan.margin_eur}  ({plan.margin_pct_of_equity}% of equity)"
        f"  ·  max {plan.max_leverage}x",
        "",
        f"  costs          €{plan.costs.total_eur}  ({plan.costs.cost_pct_of_risk}% of risk)",
        f"    spread       {plan.costs.spread_pips} pips = €{plan.costs.spread_cost_eur}",
        f"    basis        {plan.costs.spread_basis}",
        f"    rollover     {plan.costs.rollover_nights} night(s), €{plan.costs.rollover_eur}",
        "",
        "  targets        "
        + "  ".join(
            f"{target} ({pips}p, {net}R net / {gross}R gross)"
            for target, pips, net, gross in zip(
                plan.targets,
                plan.target_distances_pips,
                plan.rr_targets_net,
                plan.rr_targets,
                strict=True,
            )
        ),
        f"  expires        {plan.expires_at:%Y-%m-%d %H:%M} UTC — {plan.expiry_basis}",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fixture through the forex gate")
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--capital", help="override capital_eur (EUR)")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    parser.add_argument("--card", action="store_true", help="print the Telegram card")
    args = parser.parse_args()

    configure_logging(level="WARNING")
    decision = evaluate(args.fixture, capital_override=args.capital)

    if args.json:
        print(json.dumps(decision.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    if args.card and decision.plan is not None:
        record = SignalRecord(plan=decision.plan, user_id=0, number=1, market=Market.FOREX)
        print(forex_signal_card(record, ZoneInfo("Europe/Vilnius")))
        return
    print(describe(decision))


if __name__ == "__main__":
    main()
