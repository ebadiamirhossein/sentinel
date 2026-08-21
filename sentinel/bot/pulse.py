"""``/pulse`` — what the pipeline did, told the same way to everybody (M8.4).

Every other command in this bot is either **per user** (``/positions``, ``/stats``,
``/capital``) or **owner only** (``/status``, ``/settings``). ``/pulse`` is neither,
and that is the whole point: one analysis is bought per cycle and shared by everyone
approved (M8.1 §2), so the *reasoning* behind it is the one thing in this system that
genuinely is identical for every reader. Sizing, decisions and statistics are not, and
none of them appear here.

journal/M8_1_REPORT.md §13 named the gap this closes before it existed: a member
"cannot tell 'quiet market' from 'system down'". M8.2 then made the system
deliberately quieter — hourly cycles, ``screener_v2``, ``RECENTLY_ANALYSED``, a hard
margin budget — so a silent phone is now the *expected* state and there was nothing
anybody could type to check it.

**Read-only.** Nothing here writes, and no table was added for it: the screener's
verdicts come out of the ``llm_calls`` audit row, the skips out of M8.2's
``cycles.skipped`` column, and the per-cycle cost off the ``cycles`` row itself.

---

**Two closed classifications carry the privacy boundary, and both have meta-tests**
(``tests/bot/test_pulse.py``), following ``auth.TABLE`` and ``tracker/machine.py``:
where silence or omission is the correct behaviour, the set of cases must be a list
nobody can leave a hole in.

1. :data:`SHARED_REASONS` / :data:`PERSONAL_REASONS` split ``RejectionReason`` by what
   the code is a fact *about*. Only the shared half is ever named.
2. :data:`SKIP_WORDING` renders every ``SkipReason`` in words that name nobody.

Arithmetic lives here rather than in ``cards.py``: ``tests/bot/test_no_arithmetic.py``
scans the renderer and would fail on a single ``len(x) - len(y)``. Counting is the same
rule M6 set for money — the card renders what it is handed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from annotated_types import MaxLen
from pydantic import ValidationError

from sentinel.analyst.models import AnalystReport, CandidateStatus, SetupType
from sentinel.bot.views import (
    PulseDayView,
    PulseGateView,
    PulseSkipView,
    PulseSymbolView,
    PulseVerdictView,
    PulseView,
    SpendView,
    SymbolPulseView,
)
from sentinel.core.logging import get_logger
from sentinel.risk.models import GateStatus, RejectionReason
from sentinel.risk.rounding import money
from sentinel.screener.models import ScreenerVerdict
from sentinel.storage.models import AnalystReportRow, CycleRow, GateDecisionRow

log = get_logger(__name__)

#: How many rows one section of the card may list before it says it truncated.
#:
#: Telegram does not shorten a message over 4096 characters — it refuses to send it,
#: so the pulse would simply not arrive. This card has four lists and the analyst
#: block costs two lines each, and ``tests/bot/test_pulse`` fills every section to
#: this cap with maximum-length text and asserts the result still fits. Eight is what
#: survives that; in practice ``screener_v2`` escalates 0-2 a cycle and every other
#: section is bounded by the escalations, so the cap is a guard rather than a filter
#: — and when it does bite, the card says how many rows it dropped.
MAX_ROWS = 8

#: A stored thesis runs to 1024 characters and a pulse lists several. One line each,
#: cut at a word — the full text is in ``analyst_reports``, this is a card.
THESIS_CHARS = 110

#: A screener reason is capped at 200 by the model. Shorter here for the same reason
#: as the thesis: several of them share one screen with three other sections.
REASON_CHARS = 90


# --------------------------------------------------------------------------- #
# Classification 1 — which gate verdicts are a fact about the analysis
# --------------------------------------------------------------------------- #

#: Rejections that are **provably** the same for every user, and may be named.
#:
#: The line is not a judgement call: it is exactly where ``RiskEngine.evaluate``
#: stops reading ``(report, market, config)`` and starts reading ``AccountState`` and
#: ``PortfolioState`` — the ``check_portfolio_rails`` call in ``risk/engine.py``.
#: Everything above it is computed from the shared analysis and the shared config, so
#: two users with different capital get the identical verdict; everything below it is
#: arithmetic on somebody's money.
SHARED_REASONS: frozenset[RejectionReason] = frozenset(
    {
        RejectionReason.NOT_A_CANDIDATE,
        RejectionReason.MISSING_PLAN_FIELDS,
        RejectionReason.ATR_UNAVAILABLE,
        RejectionReason.INSTRUMENT_META_MISSING,
        RejectionReason.FX_UNAVAILABLE,
        RejectionReason.ENTRY_ZONE_INVALID,
        RejectionReason.STOP_SIDE,
        RejectionReason.TARGET_ORDER,
        RejectionReason.ENTRY_TOO_FAR,
        RejectionReason.STOP_TOO_TIGHT,
        RejectionReason.STOP_TOO_WIDE,
        RejectionReason.RR_TOO_LOW,
        RejectionReason.LOW_CONFIDENCE,
    }
)

#: Rejections that are a fact about **one account**. Never named on a pulse card.
#:
#: Two of these look shared and are not, so the reasoning is recorded rather than
#: left to be re-derived:
#:
#: * ``PAUSED`` — the operator's system-wide ``/pause`` never reaches the gate at all;
#:   the orchestrator stops earlier, at ``SkipReason.PAUSED``. A ``PAUSED`` row in
#:   ``gate_decisions`` can therefore only be somebody's **per-user daily-loss pause**
#:   (M8.1 §5), which is as private as a P&L figure — it says they lost money today.
#: * ``NET_RR_TOO_LOW`` — cost-as-a-share-of-risk is scale-invariant, so this is
#:   *nearly* shared. But it is measured after ladder sizing and rung collapse, both
#:   of which depend on capital, so two users can disagree. The boundary has to be
#:   provable rather than nearly true, and the cost of being wrong is one-directional.
PERSONAL_REASONS: frozenset[RejectionReason] = frozenset(
    {
        RejectionReason.NO_CAPITAL,
        RejectionReason.MAX_OPEN_RISK,
        RejectionReason.MAX_POSITIONS,
        RejectionReason.DAILY_SIGNAL_CAP,
        RejectionReason.SYMBOL_COOLDOWN,
        RejectionReason.PAUSED,
        RejectionReason.MIN_NOTIONAL,
        RejectionReason.LIQ_BUFFER,
        RejectionReason.INSUFFICIENT_MARGIN,
        RejectionReason.MARGIN_BUDGET_EXCEEDED,
        RejectionReason.NET_RR_TOO_LOW,
    }
)

#: One plain sentence per shared code. The card shows the code *and* the sentence:
#: the code is what M9 groups by and what the owner recognises, the sentence is what
#: a member can actually act on.
SHARED_WORDING: dict[RejectionReason, str] = {
    RejectionReason.NOT_A_CANDIDATE: "the analyst did not call it a setup",
    RejectionReason.MISSING_PLAN_FIELDS: "the analysis came back incomplete",
    RejectionReason.ATR_UNAVAILABLE: "no ATR — the stop distance could not be checked",
    RejectionReason.INSTRUMENT_META_MISSING: "no exchange rules for this symbol",
    RejectionReason.FX_UNAVAILABLE: "no EURUSD rate — nothing could be sized in euros",
    RejectionReason.ENTRY_ZONE_INVALID: "the entry zone was not a usable range",
    RejectionReason.STOP_SIDE: "the stop was on the wrong side of the entry",
    RejectionReason.TARGET_ORDER: "the targets were not ordered beyond the entry",
    RejectionReason.ENTRY_TOO_FAR: "the entry sat too far from the last price",
    RejectionReason.STOP_TOO_TIGHT: "the stop was inside the noise band",
    RejectionReason.STOP_TOO_WIDE: "the stop was wider than the ATR ceiling",
    RejectionReason.RR_TOO_LOW: "reward-to-risk at TP1 was below the minimum",
    RejectionReason.LOW_CONFIDENCE: "confidence was below the threshold",
}

#: What a symbol shows when the gate got past every shared check and stopped on
#: something personal — or approved it for somebody. Said in full rather than left
#: blank, so a reader is never left wondering whether the row was merely omitted.
PER_ACCOUNT_WORDING = "cleared the shared checks — the rest is per-account"

#: The label the aggregate counts personal outcomes under.
PER_ACCOUNT_KEY = "per-account"

APPROVED_CODE = "APPROVED"
APPROVED_WORDING = "a plan was approved and sent"


# --------------------------------------------------------------------------- #
# Classification 2 — how a skipped symbol is described
# --------------------------------------------------------------------------- #

#: Every ``SkipReason``, in words that name nobody.
#:
#: **Keyed by string, and deliberately not importing the enum.** ``SkipReason`` lives
#: in ``sentinel/core/orchestrator.py``, and ``bot -> core`` is backwards: the
#: orchestrator wires the stages together and the bot is one of them (CLAUDE.md's
#: dependency direction). ``cycles.skipped`` stores plain strings, so this map is
#: keyed on those, and ``tests/bot/test_pulse.py`` — where a cross-import costs
#: nothing — asserts the keys are exactly the enum's members. An eighth skip reason
#: then fails a test instead of rendering as a blank line.
#:
#: **The stored ``detail`` is never rendered**, and it costs nothing to drop: every
#: one of the nine either restates its own key ("a signal is already open for this
#: symbol", "spend limit reached") or appends a raw ISO timestamp — and two of those
#: timestamps, the cooldown expiry and the last analysis, are a clock reading off
#: somebody's last trade. It stays in the column for M9, which groups on the key and
#: reads the detail when one row needs explaining.
SKIP_WORDING: dict[str, str] = {
    "OPEN_SIGNAL": "an open signal is already running on it",
    "COOLDOWN": "cooling down after a recent result",
    "DAILY_CAP": "the day's signal cap is reached",
    "RECENTLY_ANALYSED": "analysed recently and not a candidate then",
    "NO_FUNDED_USER": "nobody is set up to receive a signal",
    "PAUSED": "the system is paused",
    "SPEND_LIMIT": "the daily analysis budget is spent",
    # M10b-2. Both are forex-only today and unreachable while it is disabled, but the
    # wording map is asserted complete against SkipReason, so they are written now
    # rather than discovered missing on switch-on day.
    "MARKET_CLOSED": "the market is closed",
    "NO_DATA": "no usable market data arrived for it",
}

#: The three whose wording is derived from somebody's book rather than from the
#: pipeline's own state. ``cycles.skipped`` is already union-level — a symbol lands
#: there only when *no* eligible user could have received it
#: (``orchestrator.CycleResult.skipped``) — so no row names one person, and each is
#: phrased about the symbol ("an open signal is already running on it") rather than
#: about whoever holds it. Listed rather than merely worded carefully, so the next
#: person to edit :data:`SKIP_WORDING` knows which three sentences were weighed.
BOOK_SKIPS: frozenset[str] = frozenset({"OPEN_SIGNAL", "COOLDOWN", "DAILY_CAP"})


# --------------------------------------------------------------------------- #
# Folding rows into what a card shows
# --------------------------------------------------------------------------- #


def gate_outcome(rows: Sequence[GateDecisionRow]) -> tuple[str, str]:
    """Every ``gate_decisions`` row for one symbol, folded into one public outcome.

    There is one row per user, so this is where several verdicts about the same
    analysis become a single sentence with nobody's name on it. The order is the
    order of how much it is anybody else's business:

    1. **Approved for anyone** → say so. It names no user, carries no size, and is a
       fact about the pipeline: a plan cleared every rail somebody had.
    2. **A shared rejection** → name the code. Every user got this verdict, because
       it was computed before the gate read anybody's account.
    3. **Anything else** → :data:`PER_ACCOUNT_WORDING`. The analysis was fine and
       somebody's own rails stopped it, which is theirs to know.

    Returns ``(code, wording)``; the code is ``""`` for case 3.
    """
    reasons: set[RejectionReason] = set()
    for row in rows:
        if row.gate_status == GateStatus.APPROVED_FOR_HUMAN.value:
            return APPROVED_CODE, APPROVED_WORDING
        if row.reason is None:
            continue
        try:
            reasons.add(RejectionReason(row.reason))
        except ValueError:  # pragma: no cover — a code retired from the enum
            continue

    shared = sorted(reason for reason in reasons if reason in SHARED_REASONS)
    if shared:
        # Several users can disagree only across the shared/personal line, never
        # within the shared half — so the first is the whole shared story.
        first = shared[0]
        return first.value, SHARED_WORDING[first]
    return "", PER_ACCOUNT_WORDING


def one_line(text: str, limit: int = THESIS_CHARS) -> str:
    """Analyst or screener prose as one line. Never re-worded, only cut, and cut visibly.

    Cut at the last whole word so the tail does not read as a typo, and always marked
    with an ellipsis — a reader has to be able to tell a short thesis from a trimmed
    one. The full text stays in ``analyst_reports`` and in the ``llm_calls`` audit
    row; this is a card, not the record.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    head = flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return (head or flat[:limit]) + "…"


def _capped[T](rows: Sequence[T], section: str) -> tuple[tuple[T, ...], tuple[str, int] | None]:
    """The first :data:`MAX_ROWS`, plus what was dropped.

    Never a silent cut. journal/M8_2_REPORT.md's rule: a view that bounds its own
    coverage has to say so, or it reads as "that was everything" — which on a
    transparency command would be the one lie it exists not to tell.
    """
    if len(rows) <= MAX_ROWS:
        return tuple(rows), None
    return tuple(rows[:MAX_ROWS]), (section, len(rows) - MAX_ROWS)


def _skip_view(symbol: str, entry: Any) -> PulseSkipView:
    """One ``cycles.skipped`` entry. An unknown key degrades rather than vanishing.

    A newer orchestrator against an older bot — mid-deploy, or a rolled-back image —
    can write a reason this build has never heard of. Showing the raw key is ugly and
    correct; showing an empty line would make the symbol look like it was analysed.
    """
    raw = entry if isinstance(entry, dict) else {}
    reason = str(raw.get("reason", ""))
    wording = SKIP_WORDING.get(reason) or reason or "no reason recorded"
    return PulseSkipView(symbol=symbol, reason=reason, wording=wording)


def _skips(cycle: CycleRow) -> list[PulseSkipView]:
    skipped = cycle.skipped if isinstance(cycle.skipped, dict) else {}
    return [_skip_view(symbol, entry) for symbol, entry in sorted(skipped.items())]


def _escalated(verdicts: Iterable[ScreenerVerdict]) -> list[PulseSymbolView]:
    return [
        PulseSymbolView(
            symbol=verdict.symbol,
            direction_hint=verdict.direction_hint.value,
            reason=one_line(verdict.reason, REASON_CHARS),
        )
        for verdict in verdicts
        if verdict.interesting
    ]


# --------------------------------------------------------------------------- #
# The two views
# --------------------------------------------------------------------------- #


def pulse_view(
    cycle: CycleRow | None,
    *,
    screener: Sequence[ScreenerVerdict],
    reports: Sequence[AnalystReportRow],
    decisions: Sequence[GateDecisionRow],
    spend: SpendView | None,
) -> PulseView:
    """The last completed cycle's story.

    ``spend`` is ``None`` for a member and the owner's totals otherwise. The caller
    decides that from ``actor.is_owner``; nothing below re-checks it, because the
    view simply has no figure to print when it was not handed one.
    """
    if cycle is None:
        return PulseView(at=None, status="", dry_run=False, screened=0)

    escalated = _escalated(screener)
    verdicts = [
        PulseVerdictView(
            symbol=report.symbol,
            status=report.candidate_status,
            # ``none`` is a real ``SetupType`` and means there is no setup — which is
            # exactly what a WATCHLIST or NO_SETUP verdict is. Printing it gives
            # "WATCHLIST · conf 56 · none long"; dropping it gives a shorter true line.
            setup_type="" if report.setup_type == SetupType.NONE.value else report.setup_type,
            direction=report.direction,
            confidence=report.confidence,
            thesis=one_line(report.thesis),
        )
        for report in reports
    ]

    by_symbol: dict[str, list[GateDecisionRow]] = {}
    for decision in decisions:
        by_symbol.setdefault(decision.symbol, []).append(decision)
    gate: list[PulseGateView] = []
    for symbol in sorted(by_symbol):
        code, wording = gate_outcome(by_symbol[symbol])
        gate.append(
            PulseGateView(symbol=symbol, code=code, wording=wording, approved=code == APPROVED_CODE)
        )

    skipped = _skips(cycle)
    escalated_rows, escalated_cut = _capped(escalated, "escalated")
    skip_rows, skip_cut = _capped(skipped, "not analysed")
    verdict_rows, verdict_cut = _capped(verdicts, "analyst")
    gate_rows, gate_cut = _capped(gate, "gate")

    return PulseView(
        at=cycle.finished_at or cycle.started_at,
        status=cycle.status,
        dry_run=cycle.dry_run,
        screened=cycle.symbols_scanned,
        escalated=escalated_rows,
        skipped=skip_rows,
        verdicts=verdict_rows,
        gate=gate_rows,
        screener_silent=not screener,
        suspended_reason=cycle.suspended_reason,
        error=cycle.error,
        # Quantized like every other dollar figure in the system (`/status`, the
        # spend alerts): eight stored decimal places are false precision on a
        # number whose own label says it is an estimate.
        spend_usd=None if spend is None else money(Decimal(cycle.spend_usd_estimate)),
        spend=spend,
        truncated=tuple(
            cut for cut in (escalated_cut, skip_cut, verdict_cut, gate_cut) if cut is not None
        ),
    )


def _ranked(
    counts: Counter[str], section: str
) -> tuple[
    tuple[tuple[str, int], ...],
    tuple[str, int] | None,
]:
    """Largest first, ties alphabetical — a stable order between two reads."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return _capped(ordered, section)


def pulse_day_view(
    cycles: Sequence[CycleRow],
    *,
    since: datetime,
    started: int,
    screener: dict[Any, tuple[ScreenerVerdict, ...]],
    reports: Sequence[AnalystReportRow],
    decisions: Sequence[GateDecisionRow],
    spend: SpendView | None,
) -> PulseDayView:
    """``/pulse 24h`` — the same four sections, counted instead of listed.

    ``started`` is every cycle begun in the window, completed or not, so the header
    can report a completion ratio rather than only a total: 23 of 24 says something
    that 23 does not.

    The gate counts fold through :func:`gate_outcome` per (cycle, symbol) rather than
    per row, so one analysis contributes one outcome however many users were
    evaluated against it — the same correction M8.1 §4 made to the statistics for the
    same reason. Counting rows would make the numbers move when somebody joins.
    """
    escalations: Counter[str] = Counter()
    for verdicts in screener.values():
        escalations.update(verdict.symbol for verdict in verdicts if verdict.interesting)

    skips: Counter[str] = Counter()
    for cycle in cycles:
        for skip in _skips(cycle):
            skips[skip.reason] += 1

    verdict_counts: Counter[str] = Counter(report.candidate_status for report in reports)

    by_pair: dict[tuple[Any, str], list[GateDecisionRow]] = {}
    for decision in decisions:
        by_pair.setdefault((decision.cycle_id, decision.symbol), []).append(decision)
    gate_counts: Counter[str] = Counter()
    for rows in by_pair.values():
        code, _ = gate_outcome(rows)
        gate_counts[code or PER_ACCOUNT_KEY] += 1

    escalation_rows, escalation_cut = _ranked(escalations, "escalated")
    verdict_rows, verdict_cut = _ranked(verdict_counts, "verdicts")
    skip_rows, skip_cut = _ranked(skips, "not analysed")
    gate_rows, gate_cut = _ranked(gate_counts, "gate")

    return PulseDayView(
        since=since,
        cycles_completed=len(cycles),
        cycles_started=started,
        dry_run_cycles=len([cycle for cycle in cycles if cycle.dry_run]),
        escalations=escalation_rows,
        verdicts=verdict_rows,
        skips=skip_rows,
        gate=gate_rows,
        spend=spend,
        truncated=tuple(
            cut for cut in (escalation_cut, verdict_cut, skip_cut, gate_cut) if cut is not None
        ),
    )


#: Said when a verdict cannot be matched to a gate run at all. ``cycle_id`` is
#: nullable on ``analyst_reports``, and a blank section would read as a lost row.
GATE_UNKNOWN = "this verdict is not linked to a cycle, so its gate run cannot be found"

#: Said when the gate was never asked, which is the *expected* state for anything
#: that is not a CANDIDATE — the orchestrator returns before the gate for those.
#: Without the sentence, the commonest verdict in the system renders as a gap.
GATE_NOT_ASKED = "the gate was never asked — only a CANDIDATE reaches it"

#: And when it should have run and left nothing. Rare, and worth not smoothing over.
GATE_NO_ROWS = "no gate verdict was recorded for this cycle"


def _cap_of(field: str) -> int:
    """The analyst's declared length limit for one prose field.

    Read off :class:`AnalystReport` rather than written down here. The number is
    already stated in three places — the model, the payload's ``capped()`` validator
    and the field description the prompt sends — and a fourth copy in the bot would
    be the one nobody updates.
    """
    return next(
        constraint.max_length
        for constraint in AnalystReport.model_fields[field].metadata
        if isinstance(constraint, MaxLen)
    )


def _was_capped(text: str, field: str) -> bool:
    """Whether this text was cut by ``llm.schema.capped`` before it was stored.

    Length equal to the cap is the only signal there is — ``capped`` slices and does
    not mark — and text that lands on exactly the cap without having overrun it is
    possible but vanishingly unlikely (the live distribution sits at 476-598 against
    600). The card hedges accordingly and says where the cut came from rather than
    asserting the analyst wrote more; a reader who sees a sentence stop mid-word
    otherwise concludes this command truncated it, which is the one thing it promises
    not to do.
    """
    return len(text) == _cap_of(field)


def symbol_pulse_view(
    row: AnalystReportRow | None,
    decisions: Sequence[GateDecisionRow],
    *,
    symbol: str,
    on_watchlist: bool,
) -> SymbolPulseView:
    """One symbol's last verdict, in full — ``/pulse SOLUSDT`` (M8.5).

    The stored ``report`` JSONB is validated back through :class:`AnalystReport`
    rather than read key by key. Three of the fields this card exists to show —
    ``counter_thesis``, ``evidence`` and ``data_quality_note`` — have no broken-out
    column and live only in there, and going through the model means the card is
    reading the same contract the analyst wrote, not a dict shape it assumes.

    Nothing is shortened here. :func:`one_line` is used all over the summary card and
    is deliberately **not** used on this one: the point of a drill-down is the text
    the summary had to cut, and reaching for that helper out of habit would quietly
    turn this command back into the one it exists to complete. Length is the
    renderer's problem, and it splits rather than trims.
    """
    if row is None:
        return SymbolPulseView(symbol=symbol, at=None, on_watchlist=on_watchlist)

    # A row that will not validate still has its broken-out columns, and those carry
    # the verdict itself. Degrade to them rather than refusing the whole card: a
    # later schema change must not make old verdicts unreadable (the same posture
    # `screener_verdicts_of` takes one table over).
    report: AnalystReport | None
    try:
        report = AnalystReport.model_validate(row.report)
    except ValidationError:
        log.warning(
            "bot.report_unreadable",
            symbol=symbol,
            detail="stored report does not match the current model; showing columns only",
        )
        report = None

    gate, note = _gate_for_symbol(row, decisions)
    return SymbolPulseView(
        symbol=row.symbol,
        at=row.created_at,
        on_watchlist=on_watchlist,
        status=row.candidate_status,
        setup_type="" if row.setup_type == SetupType.NONE.value else row.setup_type,
        direction=row.direction,
        timeframe_label="" if report is None else report.timeframe_label.value,
        confidence=row.confidence,
        thesis=row.thesis,
        thesis_capped=_was_capped(row.thesis, "thesis"),
        evidence=(
            ()
            if report is None
            else tuple((item.claim, item.source_field) for item in report.evidence)
        ),
        counter_thesis="" if report is None else report.counter_thesis,
        counter_thesis_capped=(
            report is not None and _was_capped(report.counter_thesis, "counter_thesis")
        ),
        invalidation="" if report is None else report.invalidation_text,
        data_quality_note=None if report is None else report.data_quality_note,
        prompt_version=row.prompt_version,
        model=row.model,
        gate=gate,
        gate_note=note,
    )


def _gate_for_symbol(
    row: AnalystReportRow, decisions: Sequence[GateDecisionRow]
) -> tuple[PulseGateView | None, str]:
    """The gate half, folded by the *same* function the summary card uses.

    Reusing :func:`gate_outcome` rather than re-deriving the outcome here is the
    whole reason the shared/personal boundary cannot drift between the two cards:
    there is one implementation, and the parametrized sweep over
    :data:`PERSONAL_REASONS` covers both surfaces through it.
    """
    if row.cycle_id is None:
        return None, GATE_UNKNOWN

    mine = [
        decision
        for decision in decisions
        if decision.cycle_id == row.cycle_id and decision.symbol == row.symbol
    ]
    if not mine:
        return None, (
            GATE_NO_ROWS
            if row.candidate_status == CandidateStatus.CANDIDATE.value
            else GATE_NOT_ASKED
        )

    code, wording = gate_outcome(mine)
    return (
        PulseGateView(
            symbol=row.symbol, code=code, wording=wording, approved=code == APPROVED_CODE
        ),
        "",
    )


__all__ = [
    "APPROVED_CODE",
    "APPROVED_WORDING",
    "BOOK_SKIPS",
    "GATE_NOT_ASKED",
    "GATE_NO_ROWS",
    "GATE_UNKNOWN",
    "MAX_ROWS",
    "PERSONAL_REASONS",
    "PER_ACCOUNT_KEY",
    "PER_ACCOUNT_WORDING",
    "REASON_CHARS",
    "SHARED_REASONS",
    "SHARED_WORDING",
    "SKIP_WORDING",
    "gate_outcome",
    "one_line",
    "pulse_day_view",
    "pulse_view",
    "symbol_pulse_view",
]
