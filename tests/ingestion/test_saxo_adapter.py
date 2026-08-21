"""The Saxo FxSpot adapter — docs/specs/FOREX.md §4 and failure modes A, B and C.

Every fixture here is RECONSTRUCTED from journal/M10b_SPIKE.md rather than recorded;
``tests/fx/test_fixture_provenance.py`` enforces that each one says so.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import ForexConfig
from sentinel.core.markets import Market
from sentinel.fx.errors import DegradedRead, InstrumentUnresolved
from sentinel.ingestion.adapters.forex_saxo import SOURCE, SaxoForexAdapter
from sentinel.ingestion.errors import SourceUnavailable
from tests.conftest import cassette
from tests.ingestion.conftest import make_fetcher

#: The fixture's newest bar starts here; on Horizon=60 it closes an hour later.
NEWEST_BAR = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)
CLOSES_AT = NEWEST_BAR + timedelta(minutes=60)
#: Comfortably past the grace period, so the whole tail counts as closed.
AFTER_CLOSE = CLOSES_AT + timedelta(minutes=5)


class StubTokens:
    """Stands in for the M10b step-3 token manager. Records what was asked for."""

    def __init__(self, token: str = "stub-access-token") -> None:
        self.token = token
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self.token


class SaxoTransport:
    """Routes by path and records every request, so call counts are assertable."""

    def __init__(
        self,
        *,
        search: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        chart: dict[str, Any] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._search = search
        self._details = details if details is not None else cassette("saxo_ref_details.json")
        self._chart = chart if chart is not None else cassette("saxo_chart_EURUSD_60.json")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/instruments/details"):
            return httpx.Response(200, json=self._details)
        if path.endswith("/ref/v1/instruments"):
            keyword = request.url.params.get("Keywords", "EURUSD")
            payload = self._search
            if payload is None:
                payload = cassette(f"saxo_ref_instruments_{keyword}.json")
            return httpx.Response(200, json=payload)
        if path.endswith("/chart/v3/charts"):
            return httpx.Response(200, json=self._chart)
        return httpx.Response(404, json={"Message": f"unrouted {path}"})

    @property
    def chart_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/chart/v3/charts")]


async def build(
    transport: SaxoTransport,
    *,
    now: datetime = AFTER_CLOSE,
    config: ForexConfig | None = None,
) -> tuple[SaxoForexAdapter, httpx.AsyncClient, StubTokens]:
    fetcher, client = make_fetcher(httpx.MockTransport(transport.handler))
    tokens = StubTokens()
    adapter = SaxoForexAdapter(
        config or ForexConfig(),
        fetcher=fetcher,
        tokens=tokens,
        clock=FrozenClock(now),
    )
    return adapter, client, tokens


# ── instrument resolution ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "uic", "pip", "tick"),
    [
        ("EURUSD", 21, Decimal("0.0001"), Decimal("1e-05")),
        ("GBPUSD", 31, Decimal("0.0001"), Decimal("1e-05")),
        ("USDJPY", 42, Decimal("0.01"), Decimal("0.001")),
    ],
)
async def test_resolution_yields_a_cross_checked_pip(
    symbol: str, uic: int, pip: Decimal, tick: Decimal
) -> None:
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        instrument = await adapter.resolve(symbol)
    assert (instrument.uic, instrument.pip, instrument.tick_size) == (uic, pip, tick)
    assert instrument.pip == instrument.tick_size * 10


async def test_the_uic_comes_from_the_response_and_is_never_hardcoded() -> None:
    """The strongest available proof that no Uic table lives in the source.

    If EURUSD's Uic were baked in anywhere, this adapter would ignore the 777 the
    venue just told it and keep asking for 21.
    """
    search = copy.deepcopy(cassette("saxo_ref_instruments_EURUSD.json"))
    search["Data"][0]["Identifier"] = 777
    details = copy.deepcopy(cassette("saxo_ref_details.json"))
    details["Data"][0]["Uic"] = 777

    transport = SaxoTransport(search=search, details=details)
    adapter, client, _ = await build(transport)
    async with client:
        instrument = await adapter.resolve("EURUSD")
    assert instrument.uic == 777

    details_request = next(r for r in transport.requests if r.url.path.endswith("/details"))
    assert details_request.url.params["Uics"] == "777"


async def test_resolution_is_cached_in_process() -> None:
    transport = SaxoTransport()
    adapter, client, tokens = await build(transport)
    async with client:
        await adapter.resolve("EURUSD")
        await adapter.resolve("EURUSD")
    assert len(transport.requests) == 2, "the second resolve must not re-fetch"
    assert tokens.calls == 2


async def test_an_unresolvable_symbol_is_skipped_with_a_reason_never_silently() -> None:
    transport = SaxoTransport(search={"Data": []})
    adapter, client, _ = await build(transport)
    async with client:
        with pytest.raises(InstrumentUnresolved):
            await adapter.resolve("EURUSD")
        resolved = await adapter.resolve_many(("EURUSD",))
    assert resolved == {}


async def test_resolve_many_keeps_what_it_can_resolve() -> None:
    """One bad symbol must not cost the watchlist the good ones."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instruments/details"):
            return httpx.Response(200, json=cassette("saxo_ref_details.json"))
        keyword = request.url.params.get("Keywords", "")
        if keyword == "EURNOK":
            return httpx.Response(200, json={"Data": []})
        return httpx.Response(200, json=cassette(f"saxo_ref_instruments_{keyword}.json"))

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    adapter = SaxoForexAdapter(
        ForexConfig(), fetcher=fetcher, tokens=StubTokens(), clock=FrozenClock(AFTER_CLOSE)
    )
    async with client:
        resolved = await adapter.resolve_many(("EURUSD", "EURNOK", "USDJPY"))
    assert sorted(resolved) == ["EURUSD", "USDJPY"]


async def test_precision_is_taken_from_reference_data_even_when_the_chart_offers_some() -> None:
    """D-h. Chart v3 returns empty ``ChartInfo``/``DisplayAndFormat`` — but if a
    future response populated them with a different precision, the pip must not move."""
    chart = copy.deepcopy(cassette("saxo_chart_EURUSD_60.json"))
    chart["ChartInfo"] = {"Decimals": 5, "Uic": 21}
    chart["DisplayAndFormat"] = {"Decimals": 5, "Format": "AllowDecimalPips"}

    transport = SaxoTransport(chart=chart)
    adapter, client, _ = await build(transport)
    async with client:
        tail = await adapter.fetch_tail("EURUSD", "1h", 10)
        instrument = await adapter.resolve("EURUSD")
    assert instrument.decimals == 4
    assert instrument.pip == Decimal("0.0001")
    assert tail.bid.candles


# ── candles ─────────────────────────────────────────────────────────────────


async def test_bid_and_ask_series_are_both_returned_and_differ_by_the_spread() -> None:
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        tail = await adapter.fetch_tail("EURUSD", "1h", 10)

    assert tail.bid.market is Market.FOREX
    assert tail.ask.market is Market.FOREX
    assert tail.bid.source == SOURCE
    assert len(tail.bid.candles) == len(tail.ask.candles) == 10
    assert tail.forming_dropped is False

    # 1.1 pips on every bar but the 21:00Z rollover one, which carries 2.7 (spike §3).
    spreads = [
        (ask.close - bid.close) / Decimal("0.0001")
        for bid, ask in zip(tail.bid.candles, tail.ask.candles, strict=True)
    ]
    assert [s.quantize(Decimal("0.1")) for s in spreads] == [
        Decimal("1.1"),
        Decimal("1.1"),
        Decimal("1.1"),
        Decimal("2.7"),
        Decimal("1.1"),
        Decimal("1.1"),
        Decimal("1.1"),
        Decimal("1.1"),
        Decimal("1.1"),
        Decimal("1.1"),
    ]


async def test_forex_candles_carry_no_volume_at_all() -> None:
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        tail = await adapter.fetch_tail("EURUSD", "1h", 10)
    assert all(candle.volume is None for candle in tail.bid.candles)
    assert tail.bid.to_frame()["volume"].isna().all()


async def test_one_request_per_timeframe_and_no_anchor_is_ever_sent() -> None:
    """§4.4/D-d. The series is not stable across queries, so we never name an anchor
    and never make a second call that could disagree with the first."""
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        await adapter.fetch_tail("EURUSD", "1h", 10)

    assert len(transport.chart_requests) == 1
    params = transport.chart_requests[0].url.params
    assert params["Count"] == "10"
    assert params["Horizon"] == "60"
    assert params["AssetType"] == "FxSpot"
    assert "Mode" not in params
    assert "Time" not in params


async def test_a_short_return_is_a_degraded_read_not_a_shorter_tail() -> None:
    """D-e. Asking for more than exists returns what exists, with no error of its own."""
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        with pytest.raises(DegradedRead, match="degraded read, not a shorter tail"):
            await adapter.fetch_tail("EURUSD", "1h", 12)


async def test_an_over_request_is_refused_before_it_can_be_clamped() -> None:
    """D-e again, one step earlier: ``Count`` above the ceiling clamps *silently*."""
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        with pytest.raises(DegradedRead, match="ceiling"):
            await adapter.fetch_tail("EURUSD", "1h", 1201)
    assert transport.chart_requests == []


async def test_a_missing_price_field_raises_rather_than_defaulting() -> None:
    chart = copy.deepcopy(cassette("saxo_chart_EURUSD_60.json"))
    del chart["Data"][3]["HighAsk"]
    transport = SaxoTransport(chart=chart)
    adapter, client, _ = await build(transport)
    async with client:
        with pytest.raises(SourceUnavailable, match="HighAsk"):
            await adapter.fetch_tail("EURUSD", "1h", 10)


# ── the forming candle (failure mode B) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("offset_seconds", "expected_candles", "dropped"),
    [
        (-1, 9, True),  # one tick before T + H + grace
        (0, 10, False),  # exactly at it
        (1, 10, False),  # one tick after
    ],
    ids=["one-tick-before", "exactly-at", "one-tick-after"],
)
async def test_the_close_boundary_at_thirty_seconds_grace(
    offset_seconds: int, expected_candles: int, dropped: bool
) -> None:
    """§4.1: a bar stamped T on horizon H is closed iff ``now >= T + H + grace``.

    There is no closed flag to consult, so this clock rule is the whole of the
    defence against a forming bar poisoning every indicator on the newest candle.
    """
    grace = ForexConfig().candle_grace_seconds
    now = CLOSES_AT + timedelta(seconds=grace + offset_seconds)

    transport = SaxoTransport()
    adapter, client, _ = await build(transport, now=now)
    async with client:
        tail = await adapter.fetch_tail("EURUSD", "1h", 10)

    assert len(tail.bid.candles) == expected_candles
    assert tail.forming_dropped is dropped
    assert tail.requested == 10, "the count assertion is on what arrived, not on what we kept"


async def test_a_tail_of_nothing_but_forming_bars_is_a_degraded_read() -> None:
    transport = SaxoTransport()
    adapter, client, _ = await build(transport, now=NEWEST_BAR - timedelta(days=1))
    async with client:
        with pytest.raises(DegradedRead, match="still forming"):
            await adapter.fetch_tail("EURUSD", "1h", 10)


# ── bar alignment (D-k) ─────────────────────────────────────────────────────


def four_hour_chart(hours: tuple[int, ...], *, day: int) -> dict[str, Any]:
    """A synthetic 4h tail on a given UTC grid. Shape only — prices are invented."""
    rows = []
    for index, hour in enumerate(hours):
        at = datetime(2026, 1 if day == 1 else 8, 12, hour, 0, tzinfo=UTC)
        base = Decimal("1.1600") + Decimal(index) / 10000
        rows.append(
            {
                "Time": at.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
                "OpenBid": float(base),
                "OpenAsk": float(base + Decimal("0.00011")),
                "HighBid": float(base + Decimal("0.0004")),
                "HighAsk": float(base + Decimal("0.00051")),
                "LowBid": float(base - Decimal("0.0004")),
                "LowAsk": float(base - Decimal("0.00029")),
                "CloseBid": float(base + Decimal("0.0001")),
                "CloseAsk": float(base + Decimal("0.00021")),
            }
        )
    return {"Data": rows, "ChartInfo": {}, "DisplayAndFormat": {}}


@pytest.mark.parametrize(
    ("hours", "month"),
    [
        ((1, 5, 9, 13, 17, 21), 8),  # US EDT: 17:00 New York is 21:00Z
        ((2, 6, 10, 14, 18, 22), 1),  # US EST: 17:00 New York is 22:00Z
    ],
    ids=["august-edt", "january-est"],
)
async def test_the_four_hour_grid_is_read_from_the_data_never_assumed(
    hours: tuple[int, ...], month: int
) -> None:
    """D-k. Saxo's 4h bars sit on 17:00 America/New_York and move with US DST.

    A forex 4h bar therefore never aligns with a crypto one, in either season. The
    adapter records the grid it was given rather than expecting one.
    """
    chart = four_hour_chart(hours, day=month)
    transport = SaxoTransport(chart=chart)
    adapter, client, _ = await build(transport, now=datetime(2026, month, 13, 12, 0, tzinfo=UTC))
    async with client:
        tail = await adapter.fetch_tail("EURUSD", "4h", len(hours))

    assert tail.alignment_hours_utc == tuple(sorted(hours))
    assert tail.horizon_minutes == 240
    # The crypto 4h grid is 00/04/08/12/16/20. Nothing here shares it.
    assert set(tail.alignment_hours_utc).isdisjoint({0, 4, 8, 12, 16, 20})


# ── what this venue simply does not have ────────────────────────────────────


async def test_the_absent_contexts_are_none_never_zero_filled() -> None:
    transport = SaxoTransport()
    adapter, client, _ = await build(transport)
    async with client:
        assert await adapter.derivatives_context("EURUSD") is None
        assert await adapter.orderbook_snapshot("EURUSD") is None
        assert await adapter.instrument_meta("EURUSD") is None
    assert adapter.market_hours().always_open is False
    assert adapter.market_hours().venue == SOURCE
    await adapter.close()
