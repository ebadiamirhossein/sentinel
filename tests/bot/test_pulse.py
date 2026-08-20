"""``/pulse`` — the pipeline's story, and the two boundaries it must not cross (M8.4).

Three kinds of test live here, and they are not interchangeable:

* **Meta-tests over the two classifications.** ``RejectionReason`` is split into a
  shared half that ``/pulse`` names and a personal half it never does; ``SkipReason``
  is rendered through a wording map. Both splits are enumerations of an enum, and an
  enumeration nobody checks is a hole waiting for the next code to be added. This is
  ``auth.TABLE``'s meta-test and ``tracker/machine.py``'s, applied to a third table.
* **The boundaries, asserted on the rendered text.** A parametrized sweep proves no
  personal rejection code can reach a card, and the spend line proves a member's view
  has no figure to leak rather than a renderer that remembers not to print one.
* **Degradation.** No cycle, a failed cycle, a silent screener, a suspended analyst,
  an escalated symbol with no verdict. Every one of these is a state the live system
  has actually been in, and a transparency command that renders them as "nothing
  happened" would be worse than no command at all.

Rows are the **real** ORM classes, not stand-ins: a renamed column then fails here
rather than in production, and these tests are the only place the pulse read models
see the shapes they will really be handed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.cards import pulse_card, pulse_day_card
from sentinel.bot.pulse import (
    APPROVED_CODE,
    BOOK_SKIPS,
    MAX_ROWS,
    PER_ACCOUNT_KEY,
    PER_ACCOUNT_WORDING,
    PERSONAL_REASONS,
    SHARED_REASONS,
    SHARED_WORDING,
    SKIP_WORDING,
    THESIS_CHARS,
    gate_outcome,
    one_line,
    pulse_day_view,
    pulse_view,
)
from sentinel.bot.views import PulseView, SpendView
from sentinel.core.orchestrator import SkipReason
from sentinel.risk.models import GateStatus, RejectionReason
from sentinel.screener.models import DirectionHint, ScreenerVerdict
from sentinel.storage.models import AnalystReportRow, CycleRow, GateDecisionRow
from sentinel.storage.repositories import screener_verdicts_of

TELEGRAM_TEXT_LIMIT = 4096
NOW = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)
CYCLE = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def spend() -> SpendView:
    return SpendView(
        day_usd=Decimal("4.12"),
        month_usd=Decimal("31.00"),
        limit_usd=Decimal("10"),
        warn_usd=Decimal("7"),
        state="OK",
    )


def cycle_row(**kwargs: Any) -> CycleRow:
    """A finished cycle. Every column set explicitly — an unflushed ORM row has no
    server defaults, so a field left out would be ``None`` rather than its default."""
    fields: dict[str, Any] = {
        "cycle_id": CYCLE,
        "started_at": NOW,
        "finished_at": NOW,
        "status": "OK",
        "dry_run": False,
        "symbols_requested": 10,
        "symbols_scanned": 10,
        "symbols_skipped": 0,
        "skipped": {},
        "candidates": 2,
        "analyzed": 1,
        "approved": 1,
        "published": 1,
        "spend_usd_estimate": Decimal("0.2914"),
        "analysis_suspended": False,
        "suspended_reason": None,
        "error": None,
    }
    return CycleRow(**{**fields, **kwargs})


def verdict(symbol: str, *, interesting: bool = True, reason: str = "a reason") -> ScreenerVerdict:
    return ScreenerVerdict(
        symbol=symbol,
        interesting=interesting,
        direction_hint=DirectionHint.LONG if interesting else DirectionHint.UNCLEAR,
        reason=reason,
    )


def report_row(symbol: str, *, status: str = "CANDIDATE", thesis: str = "a thesis") -> Any:
    return AnalystReportRow(
        id=uuid4(),
        cycle_id=CYCLE,
        symbol=symbol,
        created_at=NOW,
        role="primary",
        provider="anthropic",
        model="claude-fable-5",
        prompt_version="fable_v1",
        candidate_status=status,
        setup_type="trend_pullback",
        direction="long",
        confidence=78,
        thesis=thesis,
        report={},
    )


def gate_row(
    symbol: str,
    *,
    reason: RejectionReason | None = None,
    approved: bool = False,
    user_id: int = 111,
    cycle_id: UUID = CYCLE,
) -> GateDecisionRow:
    return GateDecisionRow(
        id=uuid4(),
        cycle_id=cycle_id,
        user_id=user_id,
        symbol=symbol,
        evaluated_at=NOW,
        gate_status=(
            GateStatus.APPROVED_FOR_HUMAN.value if approved else GateStatus.REJECTED.value
        ),
        reason=None if reason is None else reason.value,
        message="",
        prompt_version="fable_v1",
        plan=None,
    )


def view(tz: ZoneInfo, **kwargs: Any) -> str:
    """Build and render in one step — most tests here assert on the text."""
    built = pulse_view(
        kwargs.pop("cycle", cycle_row()),
        screener=kwargs.pop("screener", ()),
        reports=kwargs.pop("reports", ()),
        decisions=kwargs.pop("decisions", ()),
        spend=kwargs.pop("spend", None),
    )
    assert not kwargs, f"unused: {sorted(kwargs)}"
    return pulse_card(built, tz)


# --------------------------------------------------------------------------- #
# The classifications — meta-tests before behaviour
# --------------------------------------------------------------------------- #


def test_every_rejection_reason_is_classified_exactly_once() -> None:
    """The whole privacy design rests on this partition being total.

    A new ``RejectionReason`` that belongs to neither set would fall through
    ``gate_outcome``'s shared filter and land in the per-account bucket — which is
    *safe*, and therefore silent: a code that should have been named would simply
    never appear, and nothing would say so. The failure this catches is a card that
    quietly explains less than it could, which no other test can see.
    """
    every_reason = set(RejectionReason)
    assert every_reason == SHARED_REASONS | PERSONAL_REASONS
    assert not SHARED_REASONS & PERSONAL_REASONS


def test_every_shared_reason_has_a_sentence() -> None:
    """The code is for the owner and for M9; the sentence is for everybody else."""
    assert set(SHARED_WORDING) == SHARED_REASONS


def test_the_wording_map_covers_every_skip_reason() -> None:
    """``SkipReason`` is keyed by string in ``bot/pulse`` to keep ``bot -> core`` out
    of the import graph, so this is the only thing tying the two together.

    An eighth reason added to the orchestrator fails here rather than rendering as a
    bare enum name on a member's phone.
    """
    assert set(SKIP_WORDING) == {reason.value for reason in SkipReason}


def test_the_book_derived_skips_are_the_three_the_design_weighed() -> None:
    """Named rather than implied: these are the three whose wording had to be chosen
    to describe the *symbol* rather than whoever's book put it there."""
    weighed = {
        SkipReason.OPEN_SIGNAL.value,
        SkipReason.COOLDOWN.value,
        SkipReason.DAILY_CAP.value,
    }
    worded = set(SKIP_WORDING)
    assert weighed == BOOK_SKIPS
    assert worded >= BOOK_SKIPS


def test_the_paused_reason_is_personal_and_not_the_operators_pause() -> None:
    """A regression guard on the subtlest entry in the table.

    ``PAUSED`` reads like a system-wide fact and is not one: the orchestrator holds
    symbols back at ``SkipReason.PAUSED`` before the gate runs, so a ``PAUSED`` row in
    ``gate_decisions`` can only be a user's own daily-loss pause — which says they
    lost money today.
    """
    assert RejectionReason.PAUSED in PERSONAL_REASONS


# --------------------------------------------------------------------------- #
# gate_outcome — several users' verdicts folded into one public sentence
# --------------------------------------------------------------------------- #


def test_an_approval_for_anyone_is_reported_as_an_approval() -> None:
    code, wording = gate_outcome([gate_row("SOLUSDT", approved=True)])
    assert code == APPROVED_CODE
    assert wording


def test_an_approval_wins_over_another_users_rejection() -> None:
    """Two users, one plan: approved for one and NO_CAPITAL for the other.

    The pipeline's answer is that the analysis produced a deliverable plan. Reporting
    the rejection instead would be reporting somebody's empty ``/capital``.
    """
    code, _ = gate_outcome(
        [
            gate_row("SOLUSDT", reason=RejectionReason.NO_CAPITAL, user_id=222),
            gate_row("SOLUSDT", approved=True, user_id=111),
        ]
    )
    assert code == APPROVED_CODE


def test_a_shared_rejection_is_named_with_its_code_and_a_sentence() -> None:
    code, wording = gate_outcome([gate_row("ETHUSDT", reason=RejectionReason.RR_TOO_LOW)])
    assert code == RejectionReason.RR_TOO_LOW.value
    assert wording == SHARED_WORDING[RejectionReason.RR_TOO_LOW]


@pytest.mark.parametrize("reason", sorted(PERSONAL_REASONS), ids=lambda r: r.value)
def test_a_personal_rejection_never_names_its_code(reason: RejectionReason) -> None:
    code, wording = gate_outcome([gate_row("ETHUSDT", reason=reason)])
    assert code == ""
    assert wording == PER_ACCOUNT_WORDING


def test_a_shared_rejection_survives_a_personal_one_alongside_it() -> None:
    """Two users can disagree only across the shared/personal line — one hits a rail
    the other has not. The shared half is still true of both, so it is still said."""
    code, _ = gate_outcome(
        [
            gate_row("ETHUSDT", reason=RejectionReason.MAX_POSITIONS, user_id=222),
            gate_row("ETHUSDT", reason=RejectionReason.LOW_CONFIDENCE, user_id=111),
        ]
    )
    assert code == RejectionReason.LOW_CONFIDENCE.value


def test_a_rejection_with_no_reason_recorded_falls_back_to_per_account() -> None:
    code, wording = gate_outcome([gate_row("ETHUSDT", reason=None)])
    assert (code, wording) == ("", PER_ACCOUNT_WORDING)


# --------------------------------------------------------------------------- #
# The boundary, on the rendered card
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reason", sorted(PERSONAL_REASONS), ids=lambda r: r.value)
def test_no_personal_rejection_code_reaches_the_card(reason: RejectionReason, tz: ZoneInfo) -> None:
    """The end-to-end form of the parametrized check above.

    ``/pulse`` is the one card in this system rendered identically for people who
    cannot see each other's books, and a rejection code is a sentence about somebody's
    account: ``MAX_OPEN_RISK`` says how much they have committed, ``NO_CAPITAL`` says
    they never set up, ``PAUSED`` says they lost money today.
    """
    card = view(tz, decisions=[gate_row("ETHUSDT", reason=reason)])
    assert reason.value not in card
    assert PER_ACCOUNT_WORDING in card


def test_the_card_carries_no_user_id(tz: ZoneInfo) -> None:
    """``gate_decisions`` rows are per user and the view object drops the column, so
    there is nothing to print. Asserted anyway: it is the cheapest possible test of
    the most consequential omission on this card."""
    card = view(
        tz,
        decisions=[
            gate_row("SOLUSDT", approved=True, user_id=987654321),
            gate_row("ETHUSDT", reason=RejectionReason.NO_CAPITAL, user_id=123456789),
        ],
    )
    assert "987654321" not in card
    assert "123456789" not in card


def test_a_member_sees_no_spend_figure_at_all(tz: ZoneInfo) -> None:
    """The LLM bill is the owner's; a member has no lever to pull about it (§7)."""
    card = view(tz, spend=None)
    assert "$" not in card
    assert "spend" not in card.lower()


def test_the_owner_sees_the_cycle_cost_and_the_day(tz: ZoneInfo, spend: SpendView) -> None:
    card = view(tz, spend=spend)
    assert "$0.29" in card, "the cycle's own cost, quantized like every other figure"
    assert "$4.12" in card
    assert "estimate, not a bill" in card


def test_the_member_view_has_no_money_field_to_leak(spend: SpendView) -> None:
    """The boundary as a **type**, the way ``UserView`` carries M8.1 §6's.

    A future card cannot start printing a member's-eye cost without a field appearing
    here first, which is a visible change with this test against it.
    """
    member = pulse_view(cycle_row(), screener=(), reports=(), decisions=(), spend=None)
    owner = pulse_view(cycle_row(), screener=(), reports=(), decisions=(), spend=spend)
    assert member.spend_usd is None and member.spend is None
    assert owner.spend_usd is not None and owner.spend is not None

    money_fields = {"spend_usd", "spend"}
    for name in PulseView.__dataclass_fields__:
        if name in money_fields:
            continue
        assert not any(word in name for word in ("usd", "eur", "capital", "risk", "pnl")), (
            f"PulseView.{name} looks like it carries money; /pulse must not"
        )


def test_the_same_cycle_renders_identically_for_a_member_and_the_owner(
    tz: ZoneInfo, spend: SpendView
) -> None:
    """The design claim, asserted rather than assumed: apart from the spend line the
    two cards are the same text, because the reasoning genuinely is the same."""
    kwargs: dict[str, Any] = {
        "screener": [verdict("SOLUSDT")],
        "reports": [report_row("SOLUSDT")],
        "decisions": [gate_row("SOLUSDT", approved=True)],
    }
    member = view(tz, **kwargs, spend=None)
    owner = view(tz, **kwargs, spend=spend)
    extra = [line for line in owner.splitlines() if line and line not in member.splitlines()]
    assert len(extra) == 1 and "$" in extra[0], f"the two cards differ by more than money: {extra}"


# --------------------------------------------------------------------------- #
# The four sections
# --------------------------------------------------------------------------- #


def test_the_card_tells_the_whole_story_of_a_cycle(tz: ZoneInfo) -> None:
    card = view(
        tz,
        cycle=cycle_row(
            skipped={"LINKUSDT": {"reason": SkipReason.OPEN_SIGNAL.value, "detail": "…"}}
        ),
        screener=[
            verdict("SOLUSDT", reason="reclaimed the 82.4 breakout on rising OI"),
            verdict("ADAUSDT", interesting=False, reason="ranging"),
        ],
        reports=[report_row("SOLUSDT", thesis="4h uptrend intact, 1h pullback into EMA50")],
        decisions=[gate_row("SOLUSDT", approved=True)],
    )
    assert "1 of 10 escalated" in card
    assert "SOLUSDT (long) — reclaimed the 82.4 breakout on rising OI" in card
    assert "LINKUSDT — an open signal is already running on it" in card
    assert "CANDIDATE" in card and "conf 78" in card and "trend_pullback" in card
    assert "4h uptrend intact, 1h pullback into EMA50" in card
    assert "✅" in card


def test_a_book_derived_skip_never_carries_its_stored_detail(tz: ZoneInfo) -> None:
    """The stored sentence for a cooldown is an expiry — a clock reading off
    somebody's last trade. It stays in the column for M9 and off the card."""
    card = view(
        tz,
        cycle=cycle_row(
            skipped={
                "LINKUSDT": {
                    "reason": SkipReason.COOLDOWN.value,
                    "detail": "on cooldown until 2026-08-20T18:00:00+00:00",
                }
            }
        ),
    )
    assert "cooling down after a recent result" in card
    assert "2026-08-20T18:00:00" not in card


def test_an_unknown_skip_reason_shows_its_key_rather_than_a_blank_line(tz: ZoneInfo) -> None:
    """A newer orchestrator against an older bot — mid-deploy, or a rollback."""
    card = view(tz, cycle=cycle_row(skipped={"LINKUSDT": {"reason": "FROM_THE_FUTURE"}}))
    assert "LINKUSDT — FROM_THE_FUTURE" in card


def test_an_escalated_symbol_with_no_verdict_is_simply_absent_from_the_analyst_block(
    tz: ZoneInfo,
) -> None:
    """``AnalystUnavailable`` writes no ``analyst_reports`` row, and the stored
    screener output is pre-reconciliation, so a symbol can be escalated and have
    nothing after it. The card must not invent a verdict for it (CLAUDE.md)."""
    card = view(tz, screener=[verdict("SOLUSDT")], reports=(), decisions=())
    assert "SOLUSDT (long)" in card
    assert "🧠" not in card, "no analyst block at all is honest; a fabricated one is not"


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def test_no_cycle_yet_says_so(tz: ZoneInfo) -> None:
    card = pulse_card(pulse_view(None, screener=(), reports=(), decisions=(), spend=None), tz)
    assert "No cycle has completed yet" in card


def test_a_failed_cycle_is_shown_as_failed_rather_than_as_a_quiet_one(tz: ZoneInfo) -> None:
    """The whole point of the command. A cycle that died after its screener has the
    same shape as a cycle that found nothing, and they are opposite facts."""
    card = view(tz, cycle=cycle_row(status="FAILED", error="Binance 451"))
    assert "FAILED" in card
    assert "failed part-way" in card
    assert "Binance 451" in card


def test_a_silent_screener_is_not_reported_as_a_quiet_market(tz: ZoneInfo) -> None:
    """``screener.batch_discarded`` defaults every symbol to not-interesting, so the
    cycle looks exactly like one where nothing was happening."""
    card = view(tz, screener=())
    assert "no usable verdict recorded" in card
    assert "degraded cycle rather than a quiet market" in card


def test_an_empty_but_working_screener_says_that_quiet_is_normal(tz: ZoneInfo) -> None:
    card = view(tz, screener=[verdict("SOLUSDT", interesting=False, reason="ranging")])
    assert "0 of 10 escalated" in card
    assert "normal answer" in card


def test_a_suspended_analyst_is_named(tz: ZoneInfo) -> None:
    card = view(tz, cycle=cycle_row(suspended_reason="daily spend limit reached"))
    assert "Deep analysis held back: daily spend limit reached" in card


def test_a_dry_run_cycle_is_flagged(tz: ZoneInfo) -> None:
    """A member in a dry run gets no signals at all, and nothing else tells them why."""
    assert "DRY RUN" in view(tz, cycle=cycle_row(dry_run=True))
    assert "DRY RUN" not in view(tz, cycle=cycle_row(dry_run=False))


# --------------------------------------------------------------------------- #
# Trimming — always visible, never silent
# --------------------------------------------------------------------------- #


def test_a_long_list_is_capped_and_says_how_much_it_dropped(tz: ZoneInfo) -> None:
    """journal/M8_2_REPORT.md's rule. A bounded view that does not say it is bounded
    reads as "that was everything", which on this card is the one lie it must not
    tell."""
    escalated = [verdict(f"SYM{index}USDT") for index in range(MAX_ROWS + 3)]
    card = view(tz, screener=escalated)
    assert card.count("USDT (long)") == MAX_ROWS
    assert "+3 more" in card


def test_a_long_thesis_is_cut_at_a_word_and_marked(tz: ZoneInfo) -> None:
    long = " ".join(["word"] * 200)
    cut = one_line(long)
    assert cut.endswith("…")
    assert len(cut) <= THESIS_CHARS + 1
    assert "wor…" not in cut, "cut at a whole word, so the tail does not read as a typo"


def test_a_short_thesis_is_not_marked_as_cut() -> None:
    assert one_line("  short   thesis ") == "short thesis"


def test_a_full_card_fits_one_telegram_message(tz: ZoneInfo, spend: SpendView) -> None:
    """4096 is a hard limit: a longer message is not truncated, it is **not sent**.

    Deliberately worse than anything the pipeline can produce — every one of the four
    sections filled past its cap, with a maximum-length reason and a maximum-length
    thesis on every row. In reality a skipped symbol has no analyst verdict and no
    gate row, so no cycle can populate all four this way. If :data:`MAX_ROWS` or
    either character cap is ever raised, this is what says so.
    """
    symbols = [f"SYM{index}USDT" for index in range(MAX_ROWS + 4)]
    card = view(
        tz,
        cycle=cycle_row(
            skipped={
                symbol: {"reason": SkipReason.RECENTLY_ANALYSED.value, "detail": "x" * 200}
                for symbol in symbols
            }
        ),
        screener=[verdict(symbol, reason=" ".join(["reason"] * 40)) for symbol in symbols],
        reports=[report_row(symbol, thesis=" ".join(["thesis"] * 170)) for symbol in symbols],
        decisions=[gate_row(symbol, reason=RejectionReason.RR_TOO_LOW) for symbol in symbols],
        spend=spend,
    )
    assert len(card) < TELEGRAM_TEXT_LIMIT


# --------------------------------------------------------------------------- #
# /pulse 24h
# --------------------------------------------------------------------------- #


def day_view(**kwargs: Any) -> Any:
    return pulse_day_view(
        kwargs.pop("cycles", [cycle_row()]),
        since=kwargs.pop("since", NOW - timedelta(hours=24)),
        started=kwargs.pop("started", 24),
        screener=kwargs.pop("screener", {}),
        reports=kwargs.pop("reports", ()),
        decisions=kwargs.pop("decisions", ()),
        spend=kwargs.pop("spend", None),
    )


def test_the_day_reports_a_completion_ratio_and_not_only_a_total(tz: ZoneInfo) -> None:
    """23 of 24 says something 23 does not: PRD G4's reliability target is a ratio."""
    card = pulse_day_card(day_view(cycles=[cycle_row()] * 23, started=24), tz)
    assert "23 of 24 cycles completed" in card


def test_the_day_counts_escalations_per_symbol(tz: ZoneInfo) -> None:
    second = UUID("22222222-2222-2222-2222-222222222222")
    card = pulse_day_card(
        day_view(
            screener={
                CYCLE: (verdict("SOLUSDT"), verdict("ADAUSDT", interesting=False)),
                second: (verdict("SOLUSDT"),),
            }
        ),
        tz,
    )
    assert "SOLUSDT x2" in card
    assert "ADAUSDT" not in card, "a symbol the screener passed on was not escalated"


def test_the_day_counts_one_gate_outcome_per_analysis_not_per_user(tz: ZoneInfo) -> None:
    """M8.1 §4's fourth hole, one surface over.

    One shared analysis produces one ``gate_decisions`` row per user. Counting rows
    would make the totals grow when somebody joins, which is not a fact about the
    market — it is the same defect that would have multiplied the win rate.
    """
    card = pulse_day_card(
        day_view(
            decisions=[
                gate_row("SOLUSDT", reason=RejectionReason.RR_TOO_LOW, user_id=111),
                gate_row("SOLUSDT", reason=RejectionReason.RR_TOO_LOW, user_id=222),
                gate_row("SOLUSDT", reason=RejectionReason.RR_TOO_LOW, user_id=333),
            ]
        ),
        tz,
    )
    assert "RR_TOO_LOW x1" in card


def test_the_day_buckets_personal_rejections_without_naming_them(tz: ZoneInfo) -> None:
    card = pulse_day_card(
        day_view(
            decisions=[
                gate_row("SOLUSDT", reason=RejectionReason.NO_CAPITAL),
                gate_row("ETHUSDT", reason=RejectionReason.MAX_OPEN_RISK),
            ]
        ),
        tz,
    )
    assert f"{PER_ACCOUNT_KEY} x2" in card
    assert "NO_CAPITAL" not in card and "MAX_OPEN_RISK" not in card


def test_the_day_counts_verdicts_and_skips_by_kind(tz: ZoneInfo) -> None:
    card = pulse_day_card(
        day_view(
            cycles=[
                cycle_row(skipped={"LINKUSDT": {"reason": SkipReason.RECENTLY_ANALYSED.value}}),
                cycle_row(skipped={"BTCUSDT": {"reason": SkipReason.RECENTLY_ANALYSED.value}}),
            ],
            reports=[report_row("SOLUSDT"), report_row("ETHUSDT", status="WATCHLIST")],
        ),
        tz,
    )
    assert "RECENTLY_ANALYSED x2" in card
    assert "CANDIDATE x1" in card and "WATCHLIST x1" in card


def test_an_empty_day_says_so_rather_than_printing_four_empty_sections(tz: ZoneInfo) -> None:
    card = pulse_day_card(day_view(cycles=[], started=0), tz)
    assert "No cycle completed in the last 24 hours." in card


def test_a_member_sees_no_spend_on_the_day_card_either(tz: ZoneInfo, spend: SpendView) -> None:
    assert "$" not in pulse_day_card(day_view(spend=None), tz)
    assert "$4.12" in pulse_day_card(day_view(spend=spend), tz)


# --------------------------------------------------------------------------- #
# Reading the screener back out of the audit row
#
# The verdicts have no table of their own — they exist only inside
# ``llm_calls.response``. ``storage.screener_verdicts_of`` is the pure half of that
# read, so it is tested here beside the code that consumes it; the SQL half needs a
# real database and lives in ``tests/bot/test_persistence.py``.
# --------------------------------------------------------------------------- #


def test_a_stored_response_parses_into_verdicts() -> None:
    parsed = screener_verdicts_of(
        {
            "parsed": {
                "verdicts": [
                    {
                        "symbol": "SOLUSDT",
                        "interesting": True,
                        "direction_hint": "long",
                        "reason": "reclaimed the breakout",
                    }
                ]
            }
        }
    )
    assert [v.symbol for v in parsed] == ["SOLUSDT"]


def test_a_response_the_model_wrote_as_prose_yields_nothing() -> None:
    """``_response_audit`` only sets ``parsed`` when the text was JSON; otherwise it
    stores ``text``. That is the shape of a batch that was about to be retried."""
    assert screener_verdicts_of({"text": "I cannot answer that."}) == []
    assert screener_verdicts_of({}) == []


def test_a_verdict_the_current_model_cannot_read_is_dropped_not_guessed() -> None:
    """A later ``screener_v3`` could add a field, and ``ScreenerVerdict`` forbids
    extras — so old rows and new rows can each fail against the other's model.

    Dropping the unreadable one and keeping the rest is the honest answer: a pulse
    that reports fewer verdicts than a cycle produced understates itself, and one
    that invents their shape lies. The others still come through, so a single bad row
    does not blank the section.
    """
    parsed = screener_verdicts_of(
        {
            "parsed": {
                "verdicts": [
                    {"symbol": "SOLUSDT", "interesting": True, "direction_hint": "long"},
                    {"symbol": "NEWUSDT", "confidence_from_the_future": 9},
                    {
                        "symbol": "LINKUSDT",
                        "interesting": False,
                        "direction_hint": "unclear",
                        "reason": "ranging",
                    },
                ]
            }
        }
    )
    assert [v.symbol for v in parsed] == ["LINKUSDT"]


def test_a_verdicts_key_that_is_not_a_list_is_refused() -> None:
    assert screener_verdicts_of({"parsed": {"verdicts": {"SOLUSDT": True}}}) == []
