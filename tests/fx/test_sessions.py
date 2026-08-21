"""Session labels, and the daylight-saving trap they are built to survive.

FOREX.md §6 and spec defect #17 (2026-08-21). The load-bearing tests here are not the
ones that check a summer Wednesday — they are the ones that check the weeks when the
US and the EU disagree about what time it is, because that is the only period in which
a config table of UTC hours and this module give different answers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sentinel.fx.sessions import (
    SESSIONS,
    Session,
    session_bands,
    session_label,
    sessions_at,
    utc_bounds,
)


def at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ── the labels themselves ───────────────────────────────────────────────────


def test_the_london_new_york_overlap_is_its_own_label() -> None:
    """§6 names four labels, and the fourth is a reading of two facts, not a third fact.

    ``sessions_at`` reports the desks that are open; ``session_label`` is the one that
    knows the pair of them has a name.
    """
    noon = at(2026, 7, 15, 13)
    assert sessions_at(noon) == (Session.LONDON, Session.NEW_YORK)
    assert session_label(noon) is Session.LONDON_NY_OVERLAP
    # The raw report never invents the composite, and never reports an absence.
    assert Session.LONDON_NY_OVERLAP not in sessions_at(noon)
    assert Session.OFF_HOURS not in sessions_at(at(2026, 7, 15, 22))


def test_a_market_hour_with_no_desk_open_is_off_hours_not_an_absence() -> None:
    """The market is open at 22:00Z on a Wednesday and no major venue is.

    That is a measured state with its own behaviour, so it gets a label. It is not the
    kind of absence §2.1 is about — nothing is missing, the desks are simply shut.
    """
    assert sessions_at(at(2026, 7, 15, 22)) == ()
    assert session_label(at(2026, 7, 15, 22)) is Session.OFF_HOURS


def test_london_wins_the_label_during_the_two_hours_it_shares_with_tokyo() -> None:
    """Tokyo and London overlap for two hours and §6 gives that no name.

    The deeper book names the hour. Both are still reported by ``sessions_at``, so
    nothing is hidden — only the single label has to choose.
    """
    both = at(2026, 7, 15, 8)
    assert sessions_at(both) == (Session.TOKYO, Session.LONDON)
    assert session_label(both) is Session.LONDON


# ── daylight saving: the whole reason this module exists ─────────────────────


@pytest.mark.parametrize(
    ("on", "expected_hours"),
    [
        (date(2026, 1, 14), 4),  # both on standard time
        (date(2026, 7, 15), 4),  # both on summer time
        (date(2026, 3, 11), 5),  # US on EDT since Mar 8, EU still on GMT until Mar 29
        (date(2026, 10, 28), 5),  # EU back on GMT since Oct 25, US on EDT until Nov 1
    ],
    ids=["january-both-standard", "july-both-summer", "march-us-only", "october-eu-back-first"],
)
def test_the_overlap_is_an_hour_longer_when_the_us_and_eu_disagree(
    on: date, expected_hours: int
) -> None:
    """Spec defect #17, and the reason UTC hours could not have been configured.

    For about three weeks each spring and one each autumn the two continents are on
    different offsets, and the London-New York overlap is **five** hours rather than
    four. A config table of UTC hours would be right for forty-four weeks a year and
    silently wrong for the rest, which is the D-k failure shape exactly.
    """
    bounds = utc_bounds(Session.LONDON_NY_OVERLAP, on)
    assert bounds is not None
    start, end = bounds
    assert end - start == timedelta(hours=expected_hours)


def test_tokyo_never_shifts_because_japan_has_no_daylight_saving() -> None:
    """The one fixed point. Tokyo is 00:00-09:00 UTC in every week of the year.

    Which is also why the Tokyo-London gap breathes: London moves around Tokyo twice a
    year while Tokyo stays put.
    """
    winter = utc_bounds(Session.TOKYO, date(2026, 1, 14))
    summer = utc_bounds(Session.TOKYO, date(2026, 7, 15))
    assert winter is not None and summer is not None
    assert (winter[0].hour, winter[1].hour) == (0, 9)
    assert (summer[0].hour, summer[1].hour) == (0, 9)


def test_new_yorks_close_is_the_same_instant_saxo_anchors_its_daily_bar_to() -> None:
    """D-k: Saxo's 1d and 4h bars are anchored to 17:00 America/New_York.

    The New York session closes at 17:00 local, so the session boundary and the bar
    boundary are the same instant in both seasons — 21:00Z in August, 22:00Z in
    January. If these ever disagree, one of the two is reading a hardcoded UTC hour.
    """
    august = utc_bounds(Session.NEW_YORK, date(2026, 8, 13))
    january = utc_bounds(Session.NEW_YORK, date(2026, 1, 13))
    assert august is not None and january is not None
    assert august[1].hour == 21
    assert january[1].hour == 22


# ── the weekend ─────────────────────────────────────────────────────────────


def test_a_weekend_instant_has_no_session() -> None:
    assert sessions_at(at(2026, 7, 18, 12)) == ()
    assert session_label(at(2026, 7, 18, 12)) is Session.OFF_HOURS
    assert utc_bounds(Session.LONDON, date(2026, 7, 18)) is None


def test_the_week_opens_three_hours_before_the_first_desk_does() -> None:
    """The forex week opens Sunday 21:00 UTC, which is 06:00 Monday in Tokyo.

    So the first hours of the week have no session at all — which is independently why
    ``week_open_quiet_hours`` declines to trade through them (§5.2).
    """
    assert session_label(at(2026, 7, 19, 21)) is Session.OFF_HOURS
    assert session_label(at(2026, 7, 20, 0)) is Session.TOKYO


# ── bands, which is what the chart shades ───────────────────────────────────


def test_bands_are_clipped_to_the_requested_window() -> None:
    start, end = at(2026, 7, 15, 14), at(2026, 7, 16, 13)
    bands = session_bands(start, end)
    assert len(bands) == 2
    assert bands[0] == (start, at(2026, 7, 15, 16))  # clipped at the left edge
    assert bands[1] == (at(2026, 7, 16, 12), end)  # clipped at the right edge


def test_a_window_spanning_a_daylight_saving_change_yields_bands_of_two_widths() -> None:
    """The property a single configured band width could not express.

    The EU returns to GMT on 2026-10-25 and the US stays on EDT until 2026-11-01, so
    the overlap is four hours on the Friday before and five on the Monday after.
    """
    bands = session_bands(at(2026, 10, 23, 0), at(2026, 10, 27, 0))
    widths = {end - start for start, end in bands}
    assert widths == {timedelta(hours=4), timedelta(hours=5)}


def test_bands_skip_the_weekend_rather_than_spanning_it() -> None:
    bands = session_bands(at(2026, 7, 17, 0), at(2026, 7, 20, 23))
    assert len(bands) == 2  # Friday and Monday, nothing between
    assert [start.date() for start, _ in bands] == [date(2026, 7, 17), date(2026, 7, 20)]


def test_an_inverted_window_is_an_error_not_an_empty_result() -> None:
    with pytest.raises(ValueError, match="precedes start"):
        session_bands(at(2026, 7, 16, 0), at(2026, 7, 15, 0))


# ── the windows are local, and nothing here is a UTC hour ───────────────────


def test_no_session_window_is_written_down_as_a_utc_hour() -> None:
    """The structural half of defect #17.

    Every window carries an IANA zone name; none of them is UTC. A future edit that
    "simplifies" one of these to a UTC offset reintroduces the defect, and this test is
    what notices.
    """
    assert {w.tz_name for w in SESSIONS} == {"Asia/Tokyo", "Europe/London", "America/New_York"}
    assert all(w.tz_name not in {"UTC", "Etc/UTC"} for w in SESSIONS)


def test_off_hours_has_no_window_of_its_own() -> None:
    """It is whatever the other three leave over, so asking for its bounds is a mistake."""
    assert utc_bounds(Session.OFF_HOURS, date(2026, 7, 15)) is None
