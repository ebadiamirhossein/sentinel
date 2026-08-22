"""``shared_only=True`` — the half of a signal card that belongs to nobody in
particular (M11p §A).

One analysis produces one ``signals`` row **per approved user**, each sized against
that user's own capital. So a Persian rewrite of a *whole* card could never be shared
between two users without showing one of them the other's position size — in the
language he trusts most. ``shared_only`` is the subtraction that makes a shared rewrite
possible at all, and these tests are what stop it becoming a lie.

**Two guards, and the second is the load-bearing one.**

A purely differential test — render twice, assert the two match — is not enough, and
the reason is HANDOFF §4 item 10's shape. If ``shared_only`` were silently ignored, two
renders would still match: they would match as two identical **full** cards, with every
per-user number in front of the assertion. So the guard that actually detects a dead
flag is the one that names the forbidden vocabulary and asserts its **absence**.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.cards import signal_card
from sentinel.bot.forex_cards import ACCOUNT_LEVEL_MARGIN, forex_signal_card
from sentinel.bot.models import SignalDecision, SignalRecord
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market
from sentinel.fx.plan import ForexPlan
from sentinel.risk.models import TradePlan
from tests.bot.telegram_html import assert_sendable
from tests.fx.forex_double import forex_plan
from tests.risk_double import account, approved_plan

TZ = ZoneInfo("Europe/Vilnius")

#: Every label that names a figure belonging to ONE user. Asserted absent by name.
#: A new per-user line on either card has an obvious place to be added here, and until
#: it is added ``test_no_per_user_vocabulary_survives`` is the thing that fails.
PER_USER_LABELS = (
    "capital",
    "Notional",
    "notional",
    "Margin",
    "margin",
    "Leverage",
    "leverage",
    "Actual risk",
    "Liq. buffer",
    "Costs",
    "Pip value",
    "Your call",
    "Signal #",
    "risk budget",
    "planned",
)


def _crypto(config: AppConfig, **kwargs: object) -> SignalRecord:
    plan: TradePlan = approved_plan(config, **kwargs)
    return SignalRecord(plan=plan, user_id=7222549221, number=42)


def _forex(config: AppConfig) -> SignalRecord:
    plan: ForexPlan = forex_plan(config)
    return SignalRecord(plan=plan, user_id=7222549221, number=7, market=Market.FOREX)


@pytest.fixture
def cards(repo_config: AppConfig) -> dict[str, str]:
    """Both markets' shared forms, so every rule below is asserted over both."""
    return {
        "crypto": signal_card(_crypto(repo_config), TZ, shared_only=True),
        "forex": forex_signal_card(_forex(repo_config), TZ, shared_only=True),
    }


# ── guard 1: nothing that belongs to one user survives ──────────────────────


@pytest.mark.parametrize("market", ["crypto", "forex"])
def test_no_per_user_vocabulary_survives(cards: dict[str, str], market: str) -> None:
    """The test that detects a flag which does nothing.

    Asserted token by token rather than as "the two renders match", because two renders
    of a dead flag match perfectly.
    """
    card = cards[market]
    present = [label for label in PER_USER_LABELS if label in card]
    assert not present, f"{market} shared card still names: {present}"


@pytest.mark.parametrize("market", ["crypto", "forex"])
def test_no_money_figure_survives(cards: dict[str, str], market: str) -> None:
    """A euro sign anywhere is a per-user amount: every EUR figure on either card is
    derived from that user's capital. This catches a new money line whose *label* is
    not yet in :data:`PER_USER_LABELS`."""
    assert "€" not in cards[market]


def test_the_quantity_and_its_unit_are_gone(repo_config: AppConfig) -> None:
    """The ladder keeps its prices and weights and loses its size.

    The rung tail is the one per-user figure that shares a line with shared content, so
    it is the one a line-level filter would have had to drop wholesale.
    """
    record = _crypto(repo_config)
    assert isinstance(record.plan, TradePlan)
    full = signal_card(record, TZ)
    shared = signal_card(record, TZ, shared_only=True)
    entry = record.plan.entries[0]
    tail = f" — {entry.qty} SOL (€{entry.notional_eur})"
    assert tail in full, "the fixture stopped rendering a rung tail; this test is now vacuous"
    assert tail not in shared
    assert f"<b>{entry.price}</b>" in shared
    assert f"{entry.weight_pct}% of risk" in shared


def test_a_decision_never_reaches_the_shared_form(repo_config: AppConfig) -> None:
    """ "Your call: Taken" is the most obviously private line on the card."""
    plan = approved_plan(repo_config)
    record = SignalRecord(plan=plan, user_id=7222549221, number=42, decision=SignalDecision.TAKEN)
    assert "Your call" in signal_card(record, TZ)
    assert "Your call" not in signal_card(record, TZ, shared_only=True)


# ── guard 2: it removes only what it claims, and changes nothing else ───────


@pytest.mark.parametrize("market", ["crypto", "forex"])
def test_every_surviving_line_is_byte_identical_to_the_full_card(
    repo_config: AppConfig, market: str
) -> None:
    """The subtraction may drop lines and shorten the two it declares. It may never
    *alter* a line it keeps — a shared card that quietly re-rendered a stop price at a
    different scale would be the defect this whole feature exists to prevent."""
    if market == "crypto":
        record = _crypto(repo_config)
        full, shared = signal_card(record, TZ), signal_card(record, TZ, shared_only=True)
        shortened: tuple[str, ...] = ("🎯 <b>Plan</b>", "fable_v1 · ", "  1)", "  2)", "  3)")
    else:
        record = _forex(repo_config)
        full = forex_signal_card(record, TZ)
        shared = forex_signal_card(record, TZ, shared_only=True)
        shortened = ("🎯 <b>Plan</b>", "fable_forex_v1 · ", "  1)", "  2)", "  3)", "📐 pip")

    full_lines = set(full.splitlines())
    for line in shared.splitlines():
        if line.startswith(shortened):
            continue
        assert line in full_lines, f"shared card invented or altered a line: {line!r}"


@pytest.mark.parametrize("market", ["crypto", "forex"])
def test_the_shared_form_keeps_what_the_summary_is_about(
    cards: dict[str, str], market: str
) -> None:
    """A subtraction that took the substance with it would pass every absence test
    above and be useless. The verdict, the thesis, the levels and the invalidation are
    the whole point of the rewrite."""
    card = cards[market]
    for kept in ("📊 <b>Thesis</b>", "🛑 <b>Stop:", "❌ Invalidation:", "🥅 TP1:", "⏳ Expires"):
        assert kept in card, kept


def test_the_forex_safety_lines_are_market_facts_and_are_kept(cards: dict[str, str]) -> None:
    """ "There is no liquidation price for this trade" sits in the margin block and is
    not a per-user figure. A reader coming from 54 cycles of crypto cards looks for a
    liquidation buffer; the sentence that says why there isn't one has to survive."""
    assert ACCOUNT_LEVEL_MARGIN in cards["forex"]


@pytest.mark.parametrize("market", ["crypto", "forex"])
def test_the_shared_form_is_valid_telegram_html(cards: dict[str, str], market: str) -> None:
    assert_sendable(cards[market], what=f"{market} shared card")


# ── the honest limit of sharing ─────────────────────────────────────────────


def test_two_capitals_do_not_share_a_shared_form_and_this_is_not_a_bug(
    repo_config: AppConfig,
) -> None:
    """**Pinning a surprise, so the next reader does not assume sharing is universal.**

    The obvious differential test — size the same analysis at €200 and at €10,000, and
    assert the two shared forms match — *fails*, and it fails for a reason that is not
    presentational: the risk engine returns a genuinely **different plan**. Sizing is
    not a pure scaling. ``min_notional = max(exchange_min, 20 USDT)``, the quantity step
    and the leverage cap all bite in absolute terms, so a small account pays a different
    share of its risk budget in costs, and on the golden BTCUSDT plan it cannot fund the
    third ladder rung at all — three rungs become two, the weights move from 40/35/25 to
    53.33/46.67, and the average fill moves with them.

    Here, on SOLUSDT, the divergence is at its smallest and is therefore the sharper
    test: every price is identical and only the **cost-derived net R multiples** move,
    by 0.01R. That is still a difference the owner would read.

    So two users are not reading one plan rendered two ways. They are reading two plans.
    Sharing one Persian text between them is correct exactly when their plans coincide
    and wrong when they do not — which is why the summary is keyed on a **hash of this
    text** rather than on an assumption that it is the same for everybody.
    """
    big = signal_card(
        _crypto(repo_config, account=account(capital_eur="10000")), TZ, shared_only=True
    )
    small = signal_card(
        _crypto(repo_config, account=account(capital_eur="200")), TZ, shared_only=True
    )
    assert big != small
    differing = [
        (left, right)
        for left, right in zip(big.splitlines(), small.splitlines(), strict=True)
        if left != right
    ]
    assert differing, "the two capitals produced identical cards; this test is vacuous"
    assert all(left.startswith("🥅 TP") for left, _ in differing), differing
    for shared_line in (
        "🛑 <b>Stop: 81.20</b> (-1.78%)",
        "  1) <b>83.10</b> (-0.36%) — 40% of risk",
    ):
        assert shared_line in big and shared_line in small


def test_the_same_plan_read_by_two_users_does_share_a_shared_form(
    repo_config: AppConfig,
) -> None:
    """The other half of the claim, and the one the cache depends on: identical sizing
    and a different signal number, decision and user id produce identical bytes."""
    plan = approved_plan(repo_config)
    mine = SignalRecord(plan=plan, user_id=7222549221, number=42, decision=SignalDecision.TAKEN)
    theirs = SignalRecord(plan=plan, user_id=1958877587, number=99)
    assert signal_card(mine, TZ, shared_only=True) == signal_card(theirs, TZ, shared_only=True)
