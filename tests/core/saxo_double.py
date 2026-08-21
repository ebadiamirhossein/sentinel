"""A Saxo transport that generates real-shaped chart responses.

The recorded fixtures are ten bars long, which is right for testing the adapter's
parsing and wrong for testing a cycle: ``fetch_tail`` asserts ``len(rows) ==
requested``, and the shipped tails are 321/1200/321/101. So this generates them —
on the venue's own grids, with the weekend simply **absent** the way the spike
measured it, and with the newest bar still forming so the drop rule is exercised.

Prices are synthetic and say so. Nothing here is evidence about the market; it is
evidence about whether the pieces compose.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sentinel.core.clock import FrozenClock
from sentinel.core.config import ForexConfig
from sentinel.ingestion.adapters.forex_saxo import SaxoForexAdapter
from tests.conftest import cassette
from tests.ingestion.conftest import make_fetcher

#: A Wednesday, mid-London, well clear of the 19:00-22:00 rollover band.
NOW = datetime(2026, 8, 12, 12, 5, tzinfo=UTC)

#: The August 4h grid — 17:00 America/New_York is 21:00Z (D-k).
FOUR_HOUR_HOURS = (1, 5, 9, 13, 17, 21)

BASE_PRICES = {"EURUSD": 1.1690, "GBPUSD": 1.3050, "USDJPY": 150.25}
#: Phase offsets so the three do not move in lockstep. All three cross the dollar and
#: are genuinely highly correlated (§9's whole concern), but a synthetic set that
#: correlates at exactly 1.0000 would make the correlation test prove nothing.
PHASE = {"EURUSD": 0.0, "GBPUSD": 0.7, "USDJPY": -1.9}
PIPS = {"EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01}
SPREAD_PIPS = {"EURUSD": 1.1, "GBPUSD": 1.8, "USDJPY": 1.5}
UIC_TO_SYMBOL = {21: "EURUSD", 31: "GBPUSD", 42: "USDJPY"}


class SyntheticSaxo:
    """Serves ``/ref`` from the recorded fixtures and ``/chart`` from a generator."""

    def __init__(
        self,
        *,
        now: datetime = NOW,
        short_by: int = 0,
        stale_hours: float = 0.0,
        unresolvable: frozenset[str] = frozenset(),
    ) -> None:
        self.now = now
        #: Return this many fewer rows than asked for — D-e's silent clamp.
        self.short_by = short_by
        #: Age every bar by this much, so the newest one is too old (§5.1, defect #14).
        self.stale_hours = stale_hours
        self.unresolvable = unresolvable
        self.chart_requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/instruments/details"):
            return httpx.Response(200, json=cassette("saxo_ref_details.json"))
        if path.endswith("/ref/v1/instruments"):
            keywords = request.url.params.get("Keywords", "")
            if keywords in self.unresolvable:
                return httpx.Response(200, json={"Data": []})
            return httpx.Response(200, json=cassette(f"saxo_ref_instruments_{keywords}.json"))
        params = dict(request.url.params)
        self.chart_requests.append(params)
        return httpx.Response(200, json=self._chart(params))

    # ── the generator ────────────────────────────────────────────────────────

    def _grid(self, horizon: int, count: int) -> list[datetime]:
        """``count`` bar starts on this horizon's grid, newest last and still forming.

        Weekends are omitted rather than zero-filled — the measured behaviour
        (journal/M10b_SPIKE.md §6), and what makes the week boundary readable as a
        hole in the series.
        """
        anchor = self.now - timedelta(hours=self.stale_hours)
        out: list[datetime] = []
        if horizon == 1440:
            cursor = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            while len(out) < count:
                if cursor.weekday() < 5:
                    out.append(cursor)
                cursor -= timedelta(days=1)
            return sorted(out)

        if horizon == 240:
            cursor = anchor.replace(minute=0, second=0, microsecond=0)
            while cursor.hour not in FOUR_HOUR_HOURS:
                cursor -= timedelta(hours=1)
            step = timedelta(hours=4)
        else:
            step = timedelta(minutes=horizon)
            minute = (anchor.minute // horizon) * horizon if horizon < 60 else 0
            cursor = anchor.replace(minute=minute, second=0, microsecond=0)

        while len(out) < count:
            if _inside_the_week(cursor):
                out.append(cursor)
            cursor -= step
        return sorted(out)

    def _chart(self, params: dict[str, str]) -> dict[str, Any]:
        symbol = UIC_TO_SYMBOL[int(params["Uic"])]
        horizon = int(params["Horizon"])
        count = max(0, int(params["Count"]) - self.short_by)
        pip, spread = PIPS[symbol], SPREAD_PIPS[symbol] * PIPS[symbol]
        base = BASE_PRICES[symbol]
        places = 5 if pip < 0.01 else 3

        data = []
        for index, at in enumerate(self._grid(horizon, count)):
            phase = PHASE[symbol]
            drift = (
                base
                + 40 * pip * math.sin(index / 11.0 + phase)
                + 6 * pip * math.sin(index / 2.7 + 2 * phase)
            )
            body = 5 * pip * math.cos(index / 3.3 + phase)
            row: dict[str, Any] = {"Time": at.strftime("%Y-%m-%dT%H:%M:%S.000000Z")}
            for field, value in (
                ("Open", drift),
                ("High", drift + abs(body) + 8 * pip),
                ("Low", drift - abs(body) - 8 * pip),
                ("Close", drift + body),
            ):
                row[f"{field}Bid"] = round(value, places)
                row[f"{field}Ask"] = round(value + spread, places)
            data.append(row)
        return {"Data": data, "ChartInfo": None, "DisplayAndFormat": None}


def _inside_the_week(at: datetime) -> bool:
    """Sunday 21:00Z to Friday 21:00Z, which is the week the venue actually serves."""
    weekday = at.weekday()
    if weekday == 5:
        return False
    if weekday == 6:
        return at.hour >= 21
    if weekday == 4:
        return at.hour < 21
    return True


def build(transport: SyntheticSaxo) -> tuple[SaxoForexAdapter, Any]:
    """A real adapter over a synthetic venue. Nothing is mocked below the HTTP layer."""

    class _Tokens:
        async def access_token(self) -> str:
            return "stub"

    fetcher, client = make_fetcher(httpx.MockTransport(transport))
    adapter = SaxoForexAdapter(
        ForexConfig(),
        fetcher=fetcher,
        tokens=_Tokens(),
        clock=FrozenClock(transport.now),
    )
    return adapter, client
