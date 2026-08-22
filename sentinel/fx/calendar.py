"""The economic calendar and its blackouts (docs/specs/FOREX.md §8).

**There is no reachable Saxo calendar.** The feature flag says ``Calendar: true``
and all five probed paths 404 (journal/M10b_SPIKE.md §5). So the hand-maintained
YAML beside this module is not a backstop — it is the *only* source, and the rest of
§8 becomes more important rather than less, because there is no second source to
disagree with it.

That is what the staleness rail is for. An expired calendar must never silently
become "no events today": with one source, "I do not know" and "there is nothing"
are the same silence, and only one of them is safe to trade through. So a missing or
lapsed calendar **suppresses signals** and alerts, rather than degrading quietly.

Three policy decisions, all settled with the owner on 2026-08-21:

* **High-impact events only.**
* **-60 to +30 minutes, currency-matched.** A USD event blacks out all three pairs,
  because all three cross the dollar.
* **Blackouts cancel pending entry ladders** for the affected currency. Open
  positions get a warning annotation and are never closed — Sentinel informs, it
  never instructs. (Acting on that is M10c's, which owns the ladder; this module
  supplies the verdict.)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from sentinel.core.logging import get_logger
from sentinel.fx.models import ForexRejection
from sentinel.llm.untrusted import sanitize_untrusted

log = get_logger(__name__)

PACKAGE = "sentinel.fx.data"
CALENDAR_FILE = "calendar.yaml"
#: Long enough to name an event, short enough that the file cannot smuggle an essay
#: into a prompt. Overruns truncate and log, exactly as M8.2's string caps do.
MAX_NAME_LENGTH = 120


class Impact(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TimeConfidence(StrEnum):
    """Whether the issuing institution publishes a time, or we estimated one.

    **BoJ is the reason this exists.** It announces "in the afternoon of the second
    day" and gives no fixed clock time; the observed range is roughly 02:30-06:00 UTC.
    A single instant written down for it is an estimate, and an estimate that looks
    like a fact is how a blackout ends up on the wrong side of a rate decision.
    """

    EXACT = "exact"
    APPROXIMATE = "approximate"


@dataclass(frozen=True)
class CalendarEvent:
    at: datetime
    currency: str
    impact: Impact
    name: str
    #: The issuing institution's own page. **Required**, because a wrong date is a
    #: blackout that never fires — silence, which is the failure mode this repository
    #: has been bitten by most (HANDOFF §4 item 1) — and the only cheap defence is
    #: being able to re-check the date without re-researching it.
    source: str = ""
    #: ``approximate`` widens the window and says why. See :class:`TimeConfidence`.
    time_confidence: TimeConfidence = TimeConfidence.EXACT
    #: Per-event window overrides, in minutes. ``None`` means "use the configured
    #: default", which is -60/+30 (§8). An approximate event **must** set both, so the
    #: marker cannot be decorative.
    before_minutes: int | None = None
    after_minutes: int | None = None
    #: True while the date rests on secondary sources rather than on the institution's
    #: own calendar. It does not change behaviour — a plausible date is still used —
    #: it changes what the staleness alert asks the owner to do.
    needs_verification: bool = False

    def window(self, *, before_minutes: int, after_minutes: int) -> tuple[int, int]:
        """This event's blackout window, its own overrides winning over the defaults."""
        return (
            self.before_minutes if self.before_minutes is not None else before_minutes,
            self.after_minutes if self.after_minutes is not None else after_minutes,
        )


@dataclass(frozen=True)
class BlackoutVerdict:
    """Whether an instrument is inside a currency-matched high-impact window."""

    rejection: ForexRejection | None = None
    event: CalendarEvent | None = None
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True)
class CalendarHealth:
    """Whether the calendar may be trusted at all right now (§8's staleness rail)."""

    usable: bool
    coverage_until: date | None
    days_remaining: int | None
    detail: str
    #: True while coverage is still valid but running out inside ``warn_within_days``.
    #: Separate from ``usable``, because these are **different states with different
    #: outcomes** and collapsing them is the whole defect §8 guards against: expiring
    #: is a message, expired is a suppression.
    expiring: bool = False
    #: Events whose date rests on secondary sources. Carried on the health rather than
    #: left in a YAML comment, because the point of marking them was to be asked about
    #: them — a marker nothing reads is decoration.
    needs_verification: tuple[str, ...] = ()
    #: What the file says it does **not** cover, verbatim. An absent event is safer
    #: than a wrong one (no fabricated dates), but only if the absence is visible.
    known_gaps: tuple[str, ...] = ()

    @property
    def actions(self) -> tuple[str, ...]:
        """What the owner has to do, in the order it matters. Never a bare status.

        Same rule as the re-authentication alert (§3.1): a message that says a thing
        is wrong without saying which two-minute job fixes it sends the reader looking
        in the wrong place.
        """
        todo: list[str] = []
        if not self.usable or self.expiring:
            todo.append(
                "Extend sentinel/fx/data/calendar.yaml, then rebuild the image "
                "(docker compose up -d --build app) — the calendar is COPY-ed into the "
                "image, so a restart cannot pick up an edit."
            )
        todo.extend(
            f"Verify against the issuing institution: {name}" for name in self.needs_verification
        )
        todo.extend(f"Still missing from the calendar: {gap}" for gap in self.known_gaps)
        return tuple(todo)


@dataclass(frozen=True)
class EconomicCalendar:
    """A loaded calendar. Immutable, and it knows how far it claims to see."""

    events: tuple[CalendarEvent, ...]
    coverage_until: date | None
    source: str
    #: What this file knowingly does **not** contain, in the author's own words.
    #:
    #: ``coverage_until`` alone is a claim of completeness, and the shipped file cannot
    #: honestly make it: US PCE is absent because the BEA schedule could not be reached
    #: when the file was written, and no date was guessed. Rather than either claim a
    #: completeness that is false or claim no coverage at all — which suppresses every
    #: forex signal and would have made this milestone pointless — the file says
    #: "complete except these", and the staleness alert repeats them until they are
    #: filled in.
    known_gaps: tuple[str, ...] = ()

    # ── the staleness rail ───────────────────────────────────────────────────

    def health(self, now: datetime, *, warn_within_days: int) -> CalendarHealth:
        """Is this calendar current enough to be trusted?

        Unusable in three cases, and the first is the one this repository ships in:

        * **no coverage claimed** — no dates are invented in the shipped file, so
          forex cannot emit a signal until the owner populates it;
        * **coverage has lapsed** — the file is now silent about a period it does not
          cover, and that silence is indistinguishable from "nothing scheduled";
        * **coverage ends within ``warn_within_days``** — still usable, but the owner
          is told now rather than on the morning it runs out.
        """
        unverified = tuple(
            f"{event.currency} {event.name} at {event.at.isoformat()}"
            for event in self.events
            if event.needs_verification
        )
        if self.coverage_until is None:
            return CalendarHealth(
                usable=False,
                coverage_until=None,
                days_remaining=None,
                detail=(
                    "the calendar claims no coverage at all — no signals until it is "
                    "populated, because with no second source 'I do not know' and "
                    "'nothing is scheduled' are the same silence"
                ),
                needs_verification=unverified,
                known_gaps=self.known_gaps,
            )
        remaining = (self.coverage_until - now.date()).days
        if remaining < 0:
            return CalendarHealth(
                usable=False,
                coverage_until=self.coverage_until,
                days_remaining=remaining,
                detail=(
                    f"the calendar's coverage ended on {self.coverage_until.isoformat()}, "
                    f"{-remaining} day(s) ago — it is now silent about a period it does "
                    f"not cover"
                ),
                needs_verification=unverified,
                known_gaps=self.known_gaps,
            )
        if remaining <= warn_within_days:
            return CalendarHealth(
                usable=True,
                coverage_until=self.coverage_until,
                days_remaining=remaining,
                detail=(
                    f"the calendar runs out in {remaining} day(s), on "
                    f"{self.coverage_until.isoformat()} — it needs extending"
                ),
                expiring=True,
                needs_verification=unverified,
                known_gaps=self.known_gaps,
            )
        return CalendarHealth(
            usable=True,
            coverage_until=self.coverage_until,
            days_remaining=remaining,
            detail=f"covered to {self.coverage_until.isoformat()}",
            needs_verification=unverified,
            known_gaps=self.known_gaps,
        )

    # ── blackouts ────────────────────────────────────────────────────────────

    def blackout(
        self,
        symbol: str,
        now: datetime,
        *,
        before_minutes: int,
        after_minutes: int,
        warn_within_days: int,
    ) -> BlackoutVerdict:
        """§8's suppression window for one instrument.

        The health check comes **first**. A calendar that cannot be trusted does not
        get to report "no events" — that is the whole point of the rail.
        """
        health = self.health(now, warn_within_days=warn_within_days)
        if not health.usable:
            return BlackoutVerdict(rejection=ForexRejection.CALENDAR_STALE, detail=health.detail)

        currencies = currencies_of(symbol)
        for event in self.events:
            if event.impact is not Impact.HIGH or event.currency not in currencies:
                continue
            # The event's own window when it has one, the configured default otherwise.
            # BoJ publishes no announcement time — "the afternoon of the second day",
            # observed 02:30-06:00 UTC — so its entries carry -180/+60 and are marked
            # approximate. A -60/+30 window around an estimated instant is a window
            # that can miss the event entirely, which is a blackout that does not fire.
            before, after = event.window(before_minutes=before_minutes, after_minutes=after_minutes)
            if event.at - timedelta(minutes=before) <= now <= event.at + timedelta(minutes=after):
                approximate = (
                    " (announcement time is an ESTIMATE — this institution publishes none)"
                    if event.time_confidence is TimeConfidence.APPROXIMATE
                    else ""
                )
                return BlackoutVerdict(
                    rejection=ForexRejection.EVENT_BLACKOUT,
                    event=event,
                    detail=(
                        f"{event.currency} {event.name} at {event.at.isoformat()} — "
                        f"blackout runs -{before}/+{after} minutes{approximate}"
                    ),
                )
        return BlackoutVerdict()

    def affected_symbols(
        self,
        symbols: Sequence[str],
        now: datetime,
        *,
        before_minutes: int,
        after_minutes: int,
        warn_within_days: int,
    ) -> tuple[str, ...]:
        """Which of ``symbols`` are inside a blackout — the ladders to cancel (§8).

        Owner decision, 2026-08-21: a high-impact blackout **cancels** pending entry
        ladders for the affected currency rather than pausing them, because a ladder
        resting through a rate decision is an order placed on the assumption that
        nothing has changed.
        """
        return tuple(
            symbol
            for symbol in symbols
            if self.blackout(
                symbol,
                now,
                before_minutes=before_minutes,
                after_minutes=after_minutes,
                warn_within_days=warn_within_days,
            ).rejection
            is ForexRejection.EVENT_BLACKOUT
        )


def currencies_of(symbol: str) -> frozenset[str]:
    """Both sides of a pair. ``EURUSD`` is exposed to EUR *and* USD events."""
    if len(symbol) != 6:
        raise ValueError(f"cannot split {symbol!r} into two currencies")
    return frozenset({symbol[:3].upper(), symbol[3:].upper()})


def load_calendar(path: Path | None = None) -> EconomicCalendar:
    """Read the packaged calendar, or one named explicitly.

    Every field is validated and every name is sanitised. The file is edited by hand
    and its contents are external text, so M5's ``untrusted_news_data`` discipline
    applies here too: an event name is data, never an instruction, and it is defanged
    on the way in rather than trusted to be well-behaved on the way out.
    """
    if path is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = str(path)
    else:
        raw = yaml.safe_load(
            resources.files(PACKAGE).joinpath(CALENDAR_FILE).read_text(encoding="utf-8")
        )
        source = f"{PACKAGE}/{CALENDAR_FILE}"

    if not isinstance(raw, dict):
        raise ValueError(f"{source}: the calendar must be a YAML mapping")

    coverage = _coverage_date(raw.get("coverage_until"), source=source)
    events = tuple(
        sorted(
            (_event(entry, source=source) for entry in _events_list(raw, source=source)),
            key=lambda event: event.at,
        )
    )
    gaps = _known_gaps(raw.get("known_gaps"), source=source)
    log.info(
        "forex.calendar_loaded",
        source=source,
        events=len(events),
        coverage_until=None if coverage is None else coverage.isoformat(),
        approximate=sum(
            1 for event in events if event.time_confidence is TimeConfidence.APPROXIMATE
        ),
        needs_verification=sum(1 for event in events if event.needs_verification),
        known_gaps=len(gaps),
    )
    return EconomicCalendar(events=events, coverage_until=coverage, source=source, known_gaps=gaps)


def _known_gaps(value: Any, *, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source}: 'known_gaps' must be a list of strings")
    return tuple(sanitize_untrusted(item)[0] for item in value)


def _events_list(raw: dict[str, Any], *, source: str) -> list[Any]:
    events = raw.get("events")
    if events is None:
        return []
    if not isinstance(events, list):
        raise ValueError(f"{source}: 'events' must be a list")
    return events


def _coverage_date(value: Any, *, source: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"{source}: 'coverage_until' must be a date or null, got {value!r}")


def _event(entry: Any, *, source: str) -> CalendarEvent:
    if not isinstance(entry, dict):
        raise ValueError(f"{source}: every event must be a mapping, got {entry!r}")
    at = _instant(entry.get("at"), source=source)
    currency = entry.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ValueError(f"{source}: event at {at} has no three-letter currency")
    try:
        impact = Impact(str(entry.get("impact", "")).upper())
    except ValueError as exc:
        raise ValueError(
            f"{source}: event at {at} has impact {entry.get('impact')!r}; "
            f"expected one of {', '.join(i.value for i in Impact)}"
        ) from exc

    name, modified = sanitize_untrusted(str(entry.get("name", "")))
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH]
        modified = True
    if modified:
        log.warning("forex.calendar_name_sanitised", source=source, at=at.isoformat())

    # Required, and refused rather than defaulted. A blackout that never fires is
    # invisible, so the only cheap defence against a wrong date is a URL somebody can
    # re-check in ten seconds — and a field that may be omitted is a field that will be.
    raw_source = entry.get("source")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError(
            f"{source}: event {name!r} at {at.isoformat()} has no 'source'. Every entry "
            f"names the issuing institution's own page, because a wrong date is a "
            f"blackout that does not fire and nothing else would ever notice."
        )
    event_source, _ = sanitize_untrusted(raw_source.strip())

    try:
        confidence = TimeConfidence(str(entry.get("time_confidence", "exact")).lower())
    except ValueError as exc:
        raise ValueError(
            f"{source}: event at {at} has time_confidence "
            f"{entry.get('time_confidence')!r}; expected one of "
            f"{', '.join(c.value for c in TimeConfidence)}"
        ) from exc

    before = _optional_minutes(entry.get("before_minutes"), field="before_minutes", source=source)
    after = _optional_minutes(entry.get("after_minutes"), field="after_minutes", source=source)
    if confidence is TimeConfidence.APPROXIMATE and (before is None or after is None):
        raise ValueError(
            f"{source}: event {name!r} at {at.isoformat()} is marked approximate but "
            f"sets no window. An estimated instant inside the default -60/+30 can miss "
            f"its own event entirely, so the marker must carry the wider window it is "
            f"the reason for — otherwise it is decoration."
        )

    return CalendarEvent(
        at=at,
        currency=currency.upper(),
        impact=impact,
        name=name,
        source=event_source,
        time_confidence=confidence,
        before_minutes=before,
        after_minutes=after,
        needs_verification=bool(entry.get("needs_verification", False)),
    )


def _optional_minutes(value: Any, *, field: str, source: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{source}: '{field}' must be a non-negative whole number of minutes")
    return value


def _instant(value: Any, *, source: str) -> datetime:
    """An event's instant, always UTC.

    A naive datetime is refused rather than assumed to be UTC. US releases move with
    US daylight saving and EU ones with EU daylight saving, on different dates, so a
    calendar entry without a zone is a question, not a value.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        raise ValueError(f"{source}: event 'at' must be an ISO 8601 instant, got {value!r}")
    if parsed.tzinfo is None:
        raise ValueError(
            f"{source}: event at {value!r} carries no timezone. Write the UTC instant "
            f"with a trailing Z — a fixed local release time is not a fixed UTC time, "
            f"because US and EU daylight saving change on different dates."
        )
    return parsed.astimezone(UTC)


__all__ = [
    "CALENDAR_FILE",
    "MAX_NAME_LENGTH",
    "PACKAGE",
    "BlackoutVerdict",
    "CalendarEvent",
    "CalendarHealth",
    "EconomicCalendar",
    "Impact",
    "TimeConfidence",
    "currencies_of",
    "load_calendar",
]
