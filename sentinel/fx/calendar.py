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


@dataclass(frozen=True)
class CalendarEvent:
    at: datetime
    currency: str
    impact: Impact
    name: str


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


@dataclass(frozen=True)
class EconomicCalendar:
    """A loaded calendar. Immutable, and it knows how far it claims to see."""

    events: tuple[CalendarEvent, ...]
    coverage_until: date | None
    source: str

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
            )
        return CalendarHealth(
            usable=True,
            coverage_until=self.coverage_until,
            days_remaining=remaining,
            detail=f"covered to {self.coverage_until.isoformat()}",
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
            if (
                event.at - timedelta(minutes=before_minutes)
                <= now
                <= event.at + timedelta(minutes=after_minutes)
            ):
                return BlackoutVerdict(
                    rejection=ForexRejection.EVENT_BLACKOUT,
                    event=event,
                    detail=(
                        f"{event.currency} {event.name} at {event.at.isoformat()} — "
                        f"blackout runs -{before_minutes}/+{after_minutes} minutes"
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
    log.info(
        "forex.calendar_loaded",
        source=source,
        events=len(events),
        coverage_until=None if coverage is None else coverage.isoformat(),
    )
    return EconomicCalendar(events=events, coverage_until=coverage, source=source)


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
    return CalendarEvent(at=at, currency=currency.upper(), impact=impact, name=name)


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
    "currencies_of",
    "load_calendar",
]
