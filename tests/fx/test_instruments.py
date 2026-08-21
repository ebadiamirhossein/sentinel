"""The pip rule -- docs/specs/FOREX.md §4.2 and its failure mode A.

The load-bearing test in this file is
:func:`test_the_wrong_derivation_fails_loudly_on_every_pair`. A pip that is wrong
by 10x produces plausible spreads, plausible stops and no error anywhere, so
"the right derivation works" proves far less than "the wrong one is rejected".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.fx.errors import InstrumentUnresolved, PipMismatch
from sentinel.fx.instruments import (
    ForexInstrument,
    assert_pip,
    instrument_from_details,
    pip_from_decimals,
    resolve_uic,
)
from tests.conftest import cassette

RESOLVED_AT = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)

#: journal/M10b_SPIKE.md section 4. Uic, Format.Decimals, pip, TickSize.
PAIRS = [
    ("EURUSD", 21, 4, Decimal("0.0001"), Decimal("1e-05")),
    ("GBPUSD", 31, 4, Decimal("0.0001"), Decimal("1e-05")),
    ("USDJPY", 42, 2, Decimal("0.01"), Decimal("0.001")),
]


def details_for(symbol: str) -> dict[str, object]:
    payload = cassette("saxo_ref_details.json")
    for entry in payload["Data"]:
        if entry["Symbol"] == symbol:
            assert isinstance(entry, dict)
            return entry
    raise AssertionError(f"{symbol} missing from the details fixture")


@pytest.mark.parametrize(("symbol", "uic", "decimals", "pip", "tick"), PAIRS)
def test_pip_is_ten_to_the_minus_decimals_and_agrees_with_tick_size(
    symbol: str, uic: int, decimals: int, pip: Decimal, tick: Decimal
) -> None:
    """§4.2's table, end to end, from the reference-data fixture."""
    instrument = instrument_from_details(
        details_for(symbol), symbol=symbol, uic=uic, resolved_at=RESOLVED_AT
    )
    assert instrument.decimals == decimals
    assert instrument.pip == pip
    assert instrument.tick_size == tick
    # The cross-check, stated again here as an equality a reader can check by eye.
    assert instrument.pip == instrument.tick_size * 10
    assert instrument.min_trade_size == Decimal("1000.0")


@pytest.mark.parametrize(("symbol", "uic", "decimals", "pip", "tick"), PAIRS)
def test_the_wrong_derivation_fails_loudly_on_every_pair(
    symbol: str, uic: int, decimals: int, pip: Decimal, tick: Decimal
) -> None:
    """FOREX.md v1's "5 decimals" read naturally gives ``10 ** -(decimals - 1)``.

    Against the real ``Decimals=4`` that is 0.001 instead of 0.0001 -- every spread
    and every stop wrong by ten, and nothing in the output that would say so. The
    spike made exactly this mistake. It must raise.
    """
    wrong = pip_from_decimals(decimals - 1)
    assert wrong == pip * 10, "the wrong derivation should be 10x the right one"

    with pytest.raises(PipMismatch, match="factor of ten"):
        assert_pip(wrong, tick_size=tick, symbol=symbol)

    # And it cannot sneak in through the model either.
    with pytest.raises(PipMismatch):
        ForexInstrument(
            symbol=symbol,
            uic=uic,
            decimals=decimals,
            pip=wrong,
            tick_size=tick,
            min_trade_size=Decimal("1000"),
            amount_decimals=2,
            base_currency=symbol[:3],
            quote_currency=symbol[3:],
            resolved_at=RESOLVED_AT,
        )


def test_a_decimals_that_disagrees_with_the_venue_tick_is_rejected() -> None:
    """The other direction: the derivation is applied correctly to a wrong ``Decimals``.

    This is the failure the spike's reference data actually caught -- the spec said
    one thing and ``TickSize`` said another. Deriving faithfully from a wrong input
    still has to fail.
    """
    with pytest.raises(PipMismatch, match="disagrees with TickSize"):
        ForexInstrument(
            symbol="EURUSD",
            uic=21,
            decimals=5,
            pip=pip_from_decimals(5),
            tick_size=Decimal("1e-05"),
            min_trade_size=Decimal("1000"),
            amount_decimals=2,
            base_currency="EUR",
            quote_currency="USD",
            resolved_at=RESOLVED_AT,
        )


def test_precision_is_never_defaulted_when_the_payload_lacks_it() -> None:
    """§4.2/D-h: precision comes from reference data or the symbol is skipped.

    Chart v3 returns empty ``ChartInfo`` and ``DisplayAndFormat``, so there is no
    second place to look and a convention-shaped default would be the 10x bug with
    extra steps.
    """
    details = details_for("EURUSD")
    without_format = {key: value for key, value in details.items() if key != "Format"}
    with pytest.raises(InstrumentUnresolved, match=r"Format\.Decimals"):
        instrument_from_details(without_format, symbol="EURUSD", uic=21, resolved_at=RESOLVED_AT)

    empty_from_chart_v3 = {"Format": {}, "TickSize": 1e-05}
    with pytest.raises(InstrumentUnresolved, match=r"Format\.Decimals"):
        instrument_from_details(
            empty_from_chart_v3, symbol="EURUSD", uic=21, resolved_at=RESOLVED_AT
        )


def test_a_missing_tick_size_is_an_error_not_a_skipped_cross_check() -> None:
    """Without ``TickSize`` there is no cross-check, so there is no usable pip."""
    details = {key: v for key, v in details_for("EURUSD").items() if key != "TickSize"}
    with pytest.raises(InstrumentUnresolved, match="TickSize"):
        instrument_from_details(details, symbol="EURUSD", uic=21, resolved_at=RESOLVED_AT)


@pytest.mark.parametrize(("symbol", "uic", "_d", "_p", "_t"), PAIRS)
def test_uic_is_resolved_by_exact_symbol_not_by_first_result(
    symbol: str, uic: int, _d: int, _p: Decimal, _t: Decimal
) -> None:
    """The fixture carries a deliberate near match; taking the first row would pick it."""
    search = cassette(f"saxo_ref_instruments_{symbol}.json")
    assert resolve_uic(search, symbol=symbol) == uic
    near_matches = [e["Symbol"] for e in search["Data"] if e["Symbol"] != symbol]
    assert near_matches, "the fixture must contain a near match for this test to mean anything"


def test_an_unresolvable_symbol_is_named_never_silent() -> None:
    """§4.2: unresolvable instruments are skipped with a named reason."""
    search = cassette("saxo_ref_instruments_EURUSD.json")
    with pytest.raises(InstrumentUnresolved, match="EURNOK"):
        resolve_uic(search, symbol="EURNOK")
    with pytest.raises(InstrumentUnresolved, match="no Data array"):
        resolve_uic({}, symbol="EURUSD")


def test_a_non_fxspot_match_is_not_accepted() -> None:
    """The same ticker can exist as a different asset type; only FxSpot is ours."""
    search = {"Data": [{"AssetType": "CfdOnFutures", "Symbol": "EURUSD", "Identifier": 999}]}
    with pytest.raises(InstrumentUnresolved, match="no FxSpot instrument"):
        resolve_uic(search, symbol="EURUSD")
