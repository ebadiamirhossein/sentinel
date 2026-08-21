"""Session structure — the thing forex has instead of 24/7 (FOREX.md §2, §6).

Crypto trades continuously, so nothing in this system before M10b had a reason to
know what time it was anywhere in particular. Forex does: the same instrument behaves
differently at 03:00 UTC and at 13:00 UTC, and the difference has names.

**Spec defect #17, recorded 2026-08-21.** §6 asks for "session labels: Tokyo / London
/ New York / the London-NY overlap" and defines none of them — no hours, and, more
importantly, no timezone basis. Owner ruling the same day: the windows are defined in
each venue's **own** timezone and converted, never written down as UTC hours.

That is not fussiness. It is the same hazard D-k found in the bar grid: 17:00
America/New_York is 21:00Z in August and 22:00Z in January, and Saxo's 4h bars move
with it. Sessions move for the same reason, and they move *independently* — the US
and the EU change daylight saving on different dates, so for roughly two weeks each
spring and one each autumn the London-NY overlap is an hour longer or shorter than it
is the rest of the year. Japan keeps no daylight saving at all, so Tokyo never moves
and the Tokyo-London gap breathes around it.

A config table of UTC hours would be right for about forty-four weeks a year and
quietly wrong for the other eight, with nothing to notice it by. So the windows below
are local, ``zoneinfo`` does the conversion, and the UTC hours are an output rather
than an input.

Pure: no clock of its own, no I/O, no config. Everything is a function of the instant
handed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

#: ``datetime.weekday()``: Saturday and Sunday have no session in any venue. The forex
#: week opens Sunday 21:00 UTC, which is Monday 06:00 in Tokyo — before Tokyo's own
#: open, so the first session of the week starts three hours after the market does.
_WEEKEND = frozenset({5, 6})


class Session(StrEnum):
    """Which desk is at its own keyboard right now."""

    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    #: London and New York both open. The deepest liquidity of the day, and the only
    #: combination §6 names — which is why it is a label of its own rather than a
    #: derived flag.
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"
    #: The market is open and no major desk is. Not a null: "no session" is a real,
    #: measured state with its own behaviour, unlike the inputs §2.1 is about.
    OFF_HOURS = "OFF_HOURS"


@dataclass(frozen=True)
class SessionWindow:
    """One venue's working day, in that venue's own time.

    ``close_local_hour`` is exclusive, so 08:00-17:00 means the desk is counted as
    open through 16:59 and shut at 17:00 — which is what makes New York's close line
    up exactly with the 17:00 anchor Saxo's own daily bar uses (D-k).
    """

    session: Session
    tz_name: str
    open_local_hour: int
    close_local_hour: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


#: The three venues, in the order their days begin. Local hours, per defect #17's
#: ruling — these are the only numbers in this module, and none of them is a UTC hour.
SESSIONS: tuple[SessionWindow, ...] = (
    SessionWindow(Session.TOKYO, "Asia/Tokyo", 9, 18),
    SessionWindow(Session.LONDON, "Europe/London", 8, 17),
    SessionWindow(Session.NEW_YORK, "America/New_York", 8, 17),
)

#: Which single label wins when more than one desk is open. London and New York
#: together are their own thing; otherwise the deeper book names the hour, and Tokyo
#: yields to London during the two hours they share.
_LABEL_PRIORITY: tuple[Session, ...] = (
    Session.LONDON_NY_OVERLAP,
    Session.LONDON,
    Session.NEW_YORK,
    Session.TOKYO,
)


def _is_live(window: SessionWindow, instant: datetime) -> bool:
    """Is this desk open at ``instant``, judged in its own local time?"""
    local = instant.astimezone(window.tz)
    if local.weekday() in _WEEKEND:
        return False
    return window.open_local_hour <= local.hour < window.close_local_hour


def sessions_at(instant: datetime) -> tuple[Session, ...]:
    """Every venue open at ``instant``, in :data:`SESSIONS` order.

    Reports facts and nothing else: the result never contains
    :attr:`Session.LONDON_NY_OVERLAP` (which is a reading of two facts) nor
    :attr:`Session.OFF_HOURS` (which is the absence of them). Use
    :func:`session_label` for the single label a feature or a chart wants.
    """
    return tuple(window.session for window in SESSIONS if _is_live(window, instant))


def session_label(instant: datetime) -> Session:
    """The one label for this instant, overlap collapsed."""
    live = set(sessions_at(instant))
    if not live:
        return Session.OFF_HOURS
    if Session.LONDON in live and Session.NEW_YORK in live:
        live = {Session.LONDON_NY_OVERLAP}
    for candidate in _LABEL_PRIORITY:
        if candidate in live:
            return candidate
    return Session.OFF_HOURS  # pragma: no cover - _LABEL_PRIORITY covers every member


def _window_bounds(window: SessionWindow, on_date: date) -> tuple[datetime, datetime]:
    """One venue's day as a UTC interval. The DST shift happens right here."""
    opened = datetime(
        on_date.year, on_date.month, on_date.day, window.open_local_hour, tzinfo=window.tz
    )
    closed = datetime(
        on_date.year, on_date.month, on_date.day, window.close_local_hour, tzinfo=window.tz
    )
    return opened.astimezone(UTC), closed.astimezone(UTC)


def utc_bounds(session: Session, on_date: date) -> tuple[datetime, datetime] | None:
    """``session``'s UTC interval on its own local ``on_date``, or ``None``.

    ``None`` for a weekend date, for :attr:`Session.OFF_HOURS` (which has no window of
    its own — it is whatever the other three leave over), and for an overlap that does
    not happen on that date.

    The UTC hours this returns are **derived every time** rather than cached, because
    they are not a property of the session: they are a property of the session *on
    that date*, and they change twice a year in each hemisphere's own week.
    """
    if session is Session.OFF_HOURS:
        return None

    if session is Session.LONDON_NY_OVERLAP:
        london = utc_bounds(Session.LONDON, on_date)
        new_york = utc_bounds(Session.NEW_YORK, on_date)
        if london is None or new_york is None:
            return None
        start, end = max(london[0], new_york[0]), min(london[1], new_york[1])
        return (start, end) if start < end else None

    window = next(w for w in SESSIONS if w.session is session)
    opened, closed = _window_bounds(window, on_date)
    # The weekend test is on the *local* date, which is the date the window is named
    # for — Sunday 22:00 in London is Monday in Tokyo, and only Tokyo is working.
    if on_date.weekday() in _WEEKEND:
        return None
    return opened, closed


def session_bands(
    start: datetime, end: datetime, *, session: Session = Session.LONDON_NY_OVERLAP
) -> tuple[tuple[datetime, datetime], ...]:
    """Every occurrence of ``session`` overlapping the UTC interval ``start..end``.

    This is what the chart shades. Each band is computed from its own local date, so a
    window that spans a daylight-saving change comes back with bands of two different
    UTC widths — which is correct, and is exactly what a single config figure could
    not express.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()} precedes start {start.isoformat()}")

    bands: list[tuple[datetime, datetime]] = []
    # One day either side, because a local date's UTC interval can spill past both
    # ends of the requested window.
    cursor = (start - timedelta(days=1)).date()
    last = (end + timedelta(days=1)).date()
    while cursor <= last:
        bounds = utc_bounds(session, cursor)
        if bounds is not None and bounds[0] < end and bounds[1] > start:
            bands.append((max(bounds[0], start), min(bounds[1], end)))
        cursor += timedelta(days=1)
    return tuple(bands)


__all__ = [
    "SESSIONS",
    "Session",
    "SessionWindow",
    "session_bands",
    "session_label",
    "sessions_at",
    "utc_bounds",
]
