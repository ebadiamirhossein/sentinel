"""M4 demo: run a fixture through the risk gate and print the TradePlan.

    python -m sentinel.tools.size --fixture examples/sol_long.json
    python -m sentinel.tools.size --fixture examples/sol_long.json --json
    python -m sentinel.tools.size --fixture examples/sol_long.json --capital 500

No network, no LLM, no database: a fixture in, a decision out.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sentinel.analyst.models import AnalystReport
from sentinel.core.config import load_settings
from sentinel.core.logging import configure_logging
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    GateDecision,
    GateStatus,
    MarketContext,
    PortfolioState,
    TradePlan,
)


def load_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def build_inputs(
    payload: dict[str, Any], *, capital_override: str | None
) -> tuple[AnalystReport, MarketContext, AccountState, PortfolioState]:
    account_payload = dict(payload["account"])
    if capital_override is not None:
        account_payload["capital_eur"] = capital_override
    return (
        AnalystReport.model_validate(payload["report"]),
        MarketContext.model_validate(payload["market"]),
        AccountState.model_validate(account_payload),
        PortfolioState.model_validate(payload.get("portfolio", {})),
    )


def render(decision: GateDecision) -> str:
    if decision.plan is None:
        return (
            f"── {decision.symbol} ─────────────────────────────────────\n"
            f"{decision.status.value}  [{decision.reason.value if decision.reason else '-'}]\n"
            f"{decision.message}\n"
        )
    return _render_plan(decision.plan)


def _render_plan(plan: TradePlan) -> str:
    arrow = "LONG" if plan.direction.value == "long" else "SHORT"
    lines = [
        f"── {plan.symbol} {arrow} "
        f"[{plan.setup_type.value} · {plan.timeframe_label.value} · conf {plan.confidence}] ──",
        f"gate          {plan.gate_status.value}",
        f"capital       €{plan.capital_eur}  ·  risk {plan.risk_per_trade_pct}% "
        f"= €{plan.planned_risk_eur}  ·  EURUSD {plan.eurusd_rate}",
        "",
        f"entry ladder (limit orders)  ·  last price {plan.last_price}",
    ]
    for index, entry in enumerate(plan.entries, start=1):
        lines.append(
            f"  {index}) {entry.price} ({entry.distance_pct}%) — {entry.weight_pct}% of risk — "
            f"{entry.qty} {_base(plan.symbol)} (€{entry.notional_eur})"
        )
    lines += [
        f"  weighted entry {plan.avg_entry}  ·  avg fill {plan.avg_fill_price}",
        "",
        f"stop          {plan.stop}  (-{plan.stop_distance_pct}%)",
        *(
            f"{'targets' if i == 1 else '':<14}TP{i}: {t} (+{away}%)  {net}R net · {gross}R gross"
            for i, (t, gross, net, away) in enumerate(
                zip(
                    plan.targets,
                    plan.rr_targets,
                    plan.rr_targets_net,
                    plan.target_distances_pct,
                    strict=True,
                ),
                1,
            )
        ),
        *_cost_lines(plan),
        f"notional      €{plan.notional_eur}  ({plan.notional_usdt} USDT)",
        f"margin        €{plan.margin_eur}  ·  leverage {plan.suggested_leverage}x (isolated)",
        f"liq buffer    {'OK' if plan.liq_buffer_ok else 'FAIL'} "
        f"(liq ≈ {plan.liq_distance_pct}% vs stop {plan.stop_distance_pct}%)",
        f"actual risk   €{plan.risk_eur}  (planned €{plan.planned_risk_eur})",
        f"management    {plan.management_plan}",
        f"expires       {plan.expires_at:%Y-%m-%d %H:%M UTC}",
    ]
    return "\n".join(lines) + "\n"


def _cost_lines(plan: TradePlan) -> list[str]:
    """§4.2 — what the trade costs, before it is taken rather than after.

    Funding is always labelled an estimate, and an unavailable rate says so
    instead of rendering a reassuring €0.00.
    """
    costs = plan.costs
    fees = costs.entry_fee_eur + costs.stop_exit_fee_eur
    lines = [
        f"costs         fees €{fees}  "
        f"(maker {costs.maker_fee_pct}% in · taker {costs.taker_fee_pct}% out)",
    ]
    if not costs.funding_available:
        lines.append("              funding n/a  (no funding rate in the snapshot)")
    else:
        # normalize() strips the trailing zeros a Decimal multiply leaves behind:
        # 0.0019300% reads as false precision for a rate published to 6dp.
        rate_pct = (costs.funding_rate or Decimal(0)) * Decimal("100")
        sign = "credit " if costs.funding_eur < 0 else ""
        lines.append(
            f"              funding ~€{abs(costs.funding_eur)} {sign}est  "
            f"({rate_pct.normalize():f}% x {costs.funding_settlements} settlement(s) "
            f"@ {costs.funding_interval_hours}h)"
        )
    lines.append(
        f"              round trip €{costs.round_trip_cost_eur} "
        f"= {costs.cost_pct_of_risk}% of the €{plan.planned_risk_eur} risk budget"
    )
    return lines


def _base(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a fixture through the risk gate")
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--capital", help="override capital_eur (EUR)")
    parser.add_argument("--json", action="store_true", help="dump the full decision as JSON")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)

    report, market, account, portfolio = build_inputs(
        load_fixture(args.fixture), capital_override=args.capital
    )
    decision = RiskEngine(settings.config).evaluate(
        report=report, market=market, account=account, portfolio=portfolio
    )

    if args.json:
        print(json.dumps(decision.model_dump(mode="json"), indent=2))
    else:
        print(render(decision))
    return 0 if decision.status is GateStatus.APPROVED_FOR_HUMAN else 1


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    raise SystemExit(main())


__all__ = ["build_inputs", "load_fixture", "main", "render"]
