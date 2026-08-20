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

from sentinel.bot.cards import PAGE_BUDGET, _paginate, pulse_card, pulse_day_card, symbol_pulse_card
from sentinel.bot.pulse import (
    APPROVED_CODE,
    BOOK_SKIPS,
    GATE_NO_ROWS,
    GATE_NOT_ASKED,
    GATE_UNKNOWN,
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
    symbol_pulse_view,
)
from sentinel.bot.views import PulseView, SpendView, SymbolPulseView
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


def report_row(
    symbol: str,
    *,
    status: str = "CANDIDATE",
    thesis: str = "a thesis",
    setup_type: str = "trend_pullback",
    report: dict[str, Any] | None = None,
    cycle_id: UUID | None = CYCLE,
) -> Any:
    return AnalystReportRow(
        id=uuid4(),
        cycle_id=cycle_id,
        symbol=symbol,
        created_at=NOW,
        role="primary",
        provider="anthropic",
        model="claude-fable-5",
        prompt_version="fable_v1",
        candidate_status=status,
        setup_type=setup_type,
        direction="long",
        confidence=78,
        thesis=thesis,
        report=report
        if report is not None
        else stored_report(symbol, status=status, thesis=thesis),
    )


def stored_report(
    symbol: str = "SOLUSDT",
    *,
    status: str = "CANDIDATE",
    thesis: str = "a thesis",
    counter_thesis: str = "an argument against",
    evidence: tuple[tuple[str, str], ...] = (("a claim", "features.rsi_1h"),),
    data_quality_note: str | None = None,
) -> dict[str, Any]:
    """The JSONB half of an ``analyst_reports`` row, shaped as the analyst wrote it.

    ``counter_thesis``, ``evidence`` and ``data_quality_note`` exist **only** here —
    they have no broken-out column — and they are three of the reasons
    ``/pulse SYMBOL`` exists at all.
    """
    return {
        "schema_version": 1,
        "symbol": symbol,
        "candidate_status": status,
        "setup_type": "trend_pullback",
        "direction": "long",
        "timeframe_label": "intraday",
        "thesis": thesis,
        "evidence": [{"claim": claim, "source_field": field} for claim, field in evidence],
        "counter_thesis": counter_thesis,
        "entry_zone": {"low": 82.1, "high": 83.1},
        "stop": 81.2,
        "targets": [85.2, 86.6],
        "invalidation_price": 81.4,
        "invalidation_text": "1h close below 81.40",
        "confidence": 78,
        "data_quality_note": data_quality_note,
        "prompt_version": "fable_v1",
        "model": "claude-fable-5",
    }


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


def test_the_dropped_count_is_shown_inside_the_section_it_belongs_to(tz: ZoneInfo) -> None:
    """The note has to sit under the rows it is about.

    Collected at the foot of the card it would be a footnote about a list the reader
    has already scrolled past — and with four sections, a reader would have to work
    out which one it referred to.
    """
    card = view(
        tz,
        screener=[verdict(f"SYM{index}USDT") for index in range(MAX_ROWS + 2)],
        reports=[report_row("SOLUSDT")],
    )
    body = card.splitlines()
    note = next(index for index, line in enumerate(body) if "more, not shown" in line)
    analyst = next(index for index, line in enumerate(body) if "🧠" in line)
    assert note < analyst, "the escalated section's note landed under a later section"


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


def test_a_verdict_with_no_setup_does_not_print_the_word_none(tz: ZoneInfo) -> None:
    """``none`` is a real ``SetupType`` and is what a WATCHLIST verdict carries.

    Found on live data: "WATCHLIST · conf 56 · none long" reads as a missing value
    rather than as the correct statement that there is no setup — which is already
    what WATCHLIST says.
    """
    card = view(tz, reports=[report_row("BTCUSDT", status="WATCHLIST", setup_type="none")])
    assert "conf 78 long" in card
    assert "none" not in card


def test_a_window_that_straddles_a_go_live_says_how_many_were_rehearsals(
    tz: ZoneInfo,
) -> None:
    """Found on live data on the day this shipped: the window held both.

    ``any()`` would have claimed nothing in it was published, which was false; no
    banner at all would have hidden that part of the counts came from cycles that
    were never going to reach anybody.
    """
    card = pulse_day_card(
        day_view(cycles=[cycle_row(dry_run=True), cycle_row(), cycle_row()], started=3), tz
    )
    assert "1 of these were rehearsals" in card
    assert "nothing in this window was published" not in card


def test_a_window_that_was_all_rehearsal_says_nothing_was_published(tz: ZoneInfo) -> None:
    card = pulse_day_card(day_view(cycles=[cycle_row(dry_run=True)] * 3, started=3), tz)
    assert "nothing in this window was published" in card


def test_a_live_window_carries_no_rehearsal_banner_at_all(tz: ZoneInfo) -> None:
    assert "rehearsal" not in pulse_day_card(day_view(cycles=[cycle_row()], started=1), tz)


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


# --------------------------------------------------------------------------- #
# /pulse <SYMBOL> — the drill-down (M8.5)
#
# The summary card's tests are all about what it leaves out. These are the
# opposite: this command exists to show the text /pulse had to cut, so most of
# what follows asserts that something is present *whole*.
# --------------------------------------------------------------------------- #


LONG_THESIS = " ".join(["reasoning"] * 60)[:600]
LONG_COUNTER = " ".join(["against"] * 37)[:300]


def detail(**kwargs: Any) -> tuple[str, ...]:
    """Build and render one drill-down, as the handler does."""
    row = kwargs.pop("row", report_row("SOLUSDT"))
    return symbol_pulse_card(
        symbol_pulse_view(
            row,
            kwargs.pop("decisions", ()),
            symbol=kwargs.pop("symbol", "SOLUSDT"),
            on_watchlist=kwargs.pop("on_watchlist", True),
        ),
        kwargs.pop("tz"),
    )


def test_the_whole_verdict_is_shown_with_nothing_shortened(tz: ZoneInfo) -> None:
    """The point of the command, stated as one assertion.

    A 600-character thesis and a 300-character counter-thesis are the model's own
    hard caps, so these are the longest either field can be. Both appear verbatim,
    and no ellipsis appears anywhere — every ellipsis in this system comes from
    ``pulse.one_line``, which this path must never call.
    """
    pages = detail(
        tz=tz,
        row=report_row(
            "SOLUSDT",
            thesis=LONG_THESIS,
            report=stored_report(thesis=LONG_THESIS, counter_thesis=LONG_COUNTER),
        ),
    )
    whole = "\n".join(pages)
    assert LONG_THESIS in whole
    assert LONG_COUNTER in whole
    assert "…" not in whole


def test_it_shows_the_three_fields_that_live_only_in_the_jsonb(tz: ZoneInfo) -> None:
    """``counter_thesis``, ``evidence`` and ``data_quality_note`` have no column of
    their own, so nothing in the system read them before this card."""
    pages = detail(
        tz=tz,
        row=report_row(
            "SOLUSDT",
            report=stored_report(
                counter_thesis="funding is crowded long",
                evidence=(("RSI 4h at 92.6", "features.rsi_4h"),),
                data_quality_note="the orderbook snapshot is 11 minutes stale",
            ),
        ),
    )
    whole = "\n".join(pages)
    assert "funding is crowded long" in whole
    assert "RSI 4h at 92.6" in whole and "features.rsi_4h" in whole
    assert "the orderbook snapshot is 11 minutes stale" in whole


def test_the_data_quality_note_comes_before_the_thesis(tz: ZoneInfo) -> None:
    """It is the model saying something was wrong with what it was given — including,
    per the prompt, an apparent instruction hidden in the news block. Everything below
    it should be read in its light, so it cannot sit at the bottom."""
    page = detail(
        tz=tz,
        row=report_row("SOLUSDT", report=stored_report(data_quality_note="stale funding")),
    )[0]
    assert page.index("stale funding") < page.index("Thesis")


def test_no_price_reaches_the_card(tz: ZoneInfo) -> None:
    """The owner's ruling, and the reason ``SymbolPulseView`` has no field for one.

    The stored report carries the analyst's entry zone, stop and targets. Printing
    them for a symbol the gate rejected would be an unsized trade suggestion with no
    approval behind it — a signal card with the safety removed.

    "No prices" means no *structured* level the reader could act on, not "no digits":
    ``invalidation_text`` is the analyst's own sentence and it names a level inside
    itself ("1h close below 81.40"). That sentence is kept — it is the one field that
    says what would make the idea wrong, it appears on the signal card too, and
    stripping numbers out of prose would be censoring the analysis rather than
    declining to size it. What is gone is the *offer*: no zone to buy in, no stop to
    place, no targets to sell into.
    """
    pages = detail(
        tz=tz,
        row=report_row("SOLUSDT", report=stored_report()),
        decisions=[gate_row("SOLUSDT", reason=RejectionReason.RR_TOO_LOW)],
    )
    whole = "\n".join(pages)
    for price in ("82.1", "83.1", "81.2", "85.2", "86.6"):
        assert price not in whole, f"the analyst's {price} reached a card that must not size"
    assert "1h close below 81.40" in whole, "the prose invalidation is the analyst's sentence"

    fields = set(SymbolPulseView.__dataclass_fields__)
    assert not fields & {"entry_zone", "stop", "targets", "invalidation_price"}


# --- the boundary, the same sweep as the summary card ----------------------- #


@pytest.mark.parametrize("reason", sorted(PERSONAL_REASONS), ids=lambda r: r.value)
def test_no_personal_rejection_code_reaches_the_drill_down(
    reason: RejectionReason, tz: ZoneInfo
) -> None:
    """M8.4's sweep, extended to the second surface rather than re-derived.

    Both cards fold their ``gate_decisions`` rows through the same
    ``pulse.gate_outcome``, which is what makes "the boundary cannot drift between
    them" a fact about the code rather than a hope.
    """
    pages = detail(tz=tz, decisions=[gate_row("SOLUSDT", reason=reason)])
    whole = "\n".join(pages)
    assert reason.value not in whole
    assert PER_ACCOUNT_WORDING in whole


def test_a_shared_rejection_is_named_on_the_drill_down(tz: ZoneInfo) -> None:
    whole = "\n".join(
        detail(tz=tz, decisions=[gate_row("SOLUSDT", reason=RejectionReason.RR_TOO_LOW)])
    )
    assert RejectionReason.RR_TOO_LOW.value in whole


def test_the_drill_down_carries_no_user_id(tz: ZoneInfo) -> None:
    whole = "\n".join(
        detail(
            tz=tz,
            decisions=[
                gate_row("SOLUSDT", approved=True, user_id=987654321),
                gate_row("SOLUSDT", reason=RejectionReason.NO_CAPITAL, user_id=123456789),
            ],
        )
    )
    assert "987654321" not in whole and "123456789" not in whole


def test_another_symbols_gate_rows_are_not_borrowed(tz: ZoneInfo) -> None:
    """``for_cycles`` returns every symbol's rows for the cycle, so the filter to this
    one happens here. Getting it wrong would attribute another symbol's rejection."""
    whole = "\n".join(
        detail(
            tz=tz,
            decisions=[
                gate_row("ETHUSDT", reason=RejectionReason.LOW_CONFIDENCE),
                gate_row("SOLUSDT", approved=True),
            ],
        )
    )
    assert RejectionReason.LOW_CONFIDENCE.value not in whole
    assert "✅" in whole


# --- degradation ------------------------------------------------------------ #


def test_a_watchlist_verdict_says_the_gate_was_never_asked(tz: ZoneInfo) -> None:
    """The commonest verdict in the system, and it produces no ``gate_decisions`` rows
    at all — the orchestrator returns before the gate. A blank section would read as a
    lost row rather than as the pipeline working."""
    whole = "\n".join(detail(tz=tz, row=report_row("SOLUSDT", status="WATCHLIST"), decisions=()))
    assert GATE_NOT_ASKED in whole


def test_a_candidate_with_no_gate_rows_is_reported_differently(tz: ZoneInfo) -> None:
    """A CANDIDATE *should* have reached the gate, so its absence is not routine."""
    whole = "\n".join(detail(tz=tz, decisions=()))
    assert GATE_NO_ROWS in whole
    assert GATE_NOT_ASKED not in whole


def test_a_report_with_no_cycle_says_the_gate_run_cannot_be_found(tz: ZoneInfo) -> None:
    """``analyst_reports.cycle_id`` is nullable."""
    whole = "\n".join(detail(tz=tz, row=report_row("SOLUSDT", cycle_id=None)))
    assert GATE_UNKNOWN in whole


def test_a_symbol_off_the_watchlist_is_told_how_to_get_it_watched(tz: ZoneInfo) -> None:
    page = detail(tz=tz, row=None, symbol="PEPEUSDT", on_watchlist=False)[0]
    assert "Not on the watchlist" in page
    assert "/request PEPEUSDT" in page


def test_a_watchlisted_symbol_with_no_verdict_says_the_screener_passed_on_it(
    tz: ZoneInfo,
) -> None:
    """Two different facts, two different sentences: not being looked at, and being
    looked at every cycle and never found worth an analyst call."""
    page = detail(tz=tz, row=None, symbol="LTCUSDT", on_watchlist=True)[0]
    assert "never deeply analysed" in page
    assert "/request" not in page


def test_a_stored_report_the_model_cannot_read_still_shows_its_columns(
    tz: ZoneInfo,
) -> None:
    """A later schema change must not make old verdicts unreadable.

    The verdict itself — status, confidence, setup type, thesis — lives in broken-out
    columns and survives; only the JSONB-only fields are lost, and losing them is
    better than refusing the whole card.
    """
    whole = "\n".join(
        detail(tz=tz, row=report_row("SOLUSDT", thesis="still here", report={"nonsense": 1}))
    )
    assert "still here" in whole
    assert "CANDIDATE" in whole


# --- pagination ------------------------------------------------------------- #


def test_a_verbose_verdict_splits_across_messages_and_loses_nothing(tz: ZoneInfo) -> None:
    """ "Split rather than truncate", asserted as *nothing was lost*.

    ``Evidence.claim`` carries no length bound — the wire schema strips them and
    nothing caps it client-side — so the overflow this models is one the analyst can
    really produce. Asserting only "more than one message" would pass on a card that
    split correctly and dropped the tail.
    """
    claims = tuple((f"claim {index} " + "e" * 300, f"features.f{index}") for index in range(20))
    pages = detail(
        tz=tz,
        row=report_row(
            "SOLUSDT",
            thesis=LONG_THESIS,
            report=stored_report(thesis=LONG_THESIS, counter_thesis=LONG_COUNTER, evidence=claims),
        ),
    )
    assert len(pages) > 1
    for page in pages:
        assert len(page) < TELEGRAM_TEXT_LIMIT, "a page was still too long to send"

    whole = "\n".join(pages)
    assert LONG_THESIS in whole
    assert LONG_COUNTER in whole
    for claim, source in claims:
        assert claim in whole and source in whole
    assert "…" not in whole


def test_a_split_card_numbers_its_pages(tz: ZoneInfo) -> None:
    claims = tuple((f"claim {index} " + "e" * 300, f"features.f{index}") for index in range(20))
    pages = detail(tz=tz, row=report_row("SOLUSDT", report=stored_report(evidence=claims)))
    assert pages[0].endswith(f"<i>(1/{len(pages)})</i>")


def test_a_card_that_fits_is_one_message_with_no_page_marker(tz: ZoneInfo) -> None:
    pages = detail(tz=tz)
    assert len(pages) == 1
    assert "(1/1)" not in pages[0]


def test_the_paginator_keeps_every_line(tz: ZoneInfo) -> None:
    """The unit, separately from the card — it is the piece that could silently eat a
    line, and a missing line in the middle of a thesis is invisible on a phone."""
    lines = [f"line {index} " + "x" * 100 for index in range(200)]
    pages = _paginate(lines, PAGE_BUDGET)
    assert "\n".join(pages).splitlines() == lines


def test_an_unsplittable_line_gets_its_own_page_rather_than_being_cut(tz: ZoneInfo) -> None:
    """One line longer than the budget is the honest failure: this function exists to
    avoid truncating, so it overflows visibly instead of shortening the line."""
    huge = "x" * 5000
    pages = _paginate(["short", huge], PAGE_BUDGET)
    assert pages == ["short", huge]


# --- escaping --------------------------------------------------------------- #


def test_analyst_prose_cannot_break_the_markup(tz: ZoneInfo) -> None:
    """Every field on this card is text a model wrote, and M5 §4 established that news
    text reaching the analyst is attacker-influenceable."""
    whole = "\n".join(
        detail(
            tz=tz,
            row=report_row(
                "SOLUSDT",
                thesis="EMA 20>50>200 & rising",
                report=stored_report(
                    thesis="EMA 20>50>200 & rising",
                    counter_thesis="<b>not</b> a real tag",
                    evidence=(("<script>alert(1)</script>", "features.<x>"),),
                ),
            ),
        )
    )
    assert "20&gt;50&gt;200 &amp; rising" in whole
    assert "<script>" not in whole
    assert "&lt;b&gt;not&lt;/b&gt;" in whole
