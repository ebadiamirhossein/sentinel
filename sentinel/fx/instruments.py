"""Instrument resolution and the pip rule (docs/specs/FOREX.md §4.2).

**The pip is derived, cross-checked, and only then usable:**

    pip = 10 ** -Format.Decimals        asserted against TickSize x 10

``Format.Decimals`` is the **pip** precision -- 4 for EURUSD and GBPUSD, 2 for
USDJPY. ``Format: AllowDecimalPips`` means the venue *displays* one further
fractional-pip digit, which is why ``TickSize`` is a tenth of a pip on all three
pairs. FOREX.md v1 said "5 decimals, pip 0.0001": the values were right and the
reasoning was wrong, and the natural reading of the wrong reasoning --
``10 ** -(decimals - 1)`` -- gives 0.001 against the real ``Decimals=4``. Every
spread and every stop then comes out 10x too big, looks entirely plausible, and
raises nothing.

So the cross-check is not advice. :class:`ForexInstrument` cannot be constructed
with a pip that disagrees with ``TickSize x 10``; there is no code path that
produces an unverified pip and no field that could hold one.

Precision comes from ``/ref/v1/instruments/details`` and **nowhere else**: chart v3
returns empty ``ChartInfo`` and ``DisplayAndFormat`` objects (spike defect D-h), so
the chart payload carries no precision to read even by accident.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sentinel.fx.errors import InstrumentUnresolved, PipMismatch

#: How many ticks make one pip. ``AllowDecimalPips`` on every pair we trade means
#: the venue quotes a tenth of a pip, and this is the number the derived pip is
#: asserted against. Named rather than spelled ``10`` at the comparison, so the
#: assertion reads as the cross-check it is.
TICKS_PER_PIP = Decimal(10)


class ForexInstrument(BaseModel):
    """One resolved pair: its Uic, its verified pip, and its trading minimum.

    Frozen and validated on construction. The pip cross-check lives in the model
    rather than in the adapter because a value object that can only exist in a
    correct state cannot be passed around in an incorrect one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    uic: int
    #: ``Format.Decimals`` -- the pip precision, not the quote precision.
    decimals: int = Field(ge=0, le=8)
    pip: Decimal
    #: ``TickSize`` -- one tenth of a pip on every pair, hence the cross-check.
    tick_size: Decimal
    #: ``MinimumTradeSize``, in base-currency units. 1000.0 on all three pairs.
    min_trade_size: Decimal
    #: ``AmountDecimals`` -- the granularity a position size may be expressed to.
    #: 2 on all three pairs, with ``LotSize: null`` and ``LotSizeType: "NotUsed"``,
    #: so units are free above the minimum rather than snapped to a lot. Read from
    #: reference data like everything else here: a defaulted 2 would be a convention,
    #: and conventions are what got the pip wrong.
    amount_decimals: int = Field(ge=0, le=8)
    base_currency: str
    quote_currency: str
    resolved_at: datetime

    @model_validator(mode="after")
    def _pip_is_cross_checked(self) -> ForexInstrument:
        """Failure mode A, enforced. Both halves, because either alone can be wrong.

        The first check catches a pip that was not derived from ``Decimals`` at all.
        The second catches a ``Decimals`` that disagrees with the venue's own tick
        -- which is the direction the spike's reference data actually caught.
        """
        derived = pip_from_decimals(self.decimals)
        if self.pip != derived:
            raise PipMismatch(
                f"{self.symbol}: pip {self.pip} was not derived from Format.Decimals="
                f"{self.decimals} (10 ** -{self.decimals} = {derived})"
            )
        assert_pip(self.pip, tick_size=self.tick_size, symbol=self.symbol)
        if self.symbol[:3] != self.base_currency or self.symbol[3:] != self.quote_currency:
            raise InstrumentUnresolved(
                f"{self.symbol}: symbol does not agree with currencies "
                f"{self.base_currency}/{self.quote_currency}"
            )
        return self


def pip_from_decimals(decimals: int) -> Decimal:
    """``10 ** -decimals``, exactly (§4.2).

    ``scaleb`` rather than ``Decimal(10) ** -decimals`` so the result is exact at
    any precision and carries the scale a human would write: ``Decimal("0.0001")``,
    not a context-rounded approximation of it.
    """
    return Decimal(1).scaleb(-decimals)


def assert_pip(pip: Decimal, *, tick_size: Decimal, symbol: str = "") -> None:
    """Raise :class:`PipMismatch` unless ``pip == tick_size x 10``.

    The required assertion from §4.2, exposed as a function so it can be aimed at a
    candidate pip that never became a :class:`ForexInstrument` -- which is what the
    "a wrong derivation fails loudly" test does.
    """
    expected = tick_size * TICKS_PER_PIP
    if pip != expected:
        where = f"{symbol}: " if symbol else ""
        raise PipMismatch(
            f"{where}pip {pip} disagrees with TickSize x {TICKS_PER_PIP} "
            f"({tick_size} x {TICKS_PER_PIP} = {expected}). One of the two is wrong, "
            f"and a wrong pip is wrong by a factor of ten without looking it."
        )


def instrument_from_details(
    details: Any, *, symbol: str, uic: int, resolved_at: datetime
) -> ForexInstrument:
    """Build a :class:`ForexInstrument` from one ``/ref/v1/instruments/details`` entry.

    Every field is **required and read from the payload**. Nothing is defaulted and
    nothing is inferred: a missing ``Format.Decimals`` raises rather than falling
    back to a convention, because the whole point of §4.2 is that convention is what
    got the pip wrong in the first place. That also means the day these fixtures are
    checked against the live API (see ``tools/saxo_record_fixtures.py``), a wrong
    assumption about where a field sits surfaces as an error and not as a wrong pip.
    """
    if not isinstance(details, dict):
        raise InstrumentUnresolved(f"{symbol}: instrument details are not an object")

    fmt = details.get("Format")
    if not isinstance(fmt, dict) or "Decimals" not in fmt:
        raise InstrumentUnresolved(
            f"{symbol}: no Format.Decimals in instrument details -- precision must "
            f"come from reference data and there is nowhere else to read it (D-h)"
        )
    decimals = _require_int(fmt["Decimals"], field="Format.Decimals", symbol=symbol)
    tick_size = _require_decimal(details.get("TickSize"), field="TickSize", symbol=symbol)
    min_trade_size = _require_decimal(
        details.get("MinimumTradeSize"), field="MinimumTradeSize", symbol=symbol
    )
    amount_decimals = _require_int(
        details.get("AmountDecimals"), field="AmountDecimals", symbol=symbol
    )
    quote = details.get("CurrencyCode")
    if not isinstance(quote, str) or not quote:
        raise InstrumentUnresolved(f"{symbol}: no CurrencyCode in instrument details")

    return ForexInstrument(
        symbol=symbol,
        uic=uic,
        decimals=decimals,
        pip=pip_from_decimals(decimals),
        tick_size=tick_size,
        min_trade_size=min_trade_size,
        amount_decimals=amount_decimals,
        base_currency=symbol[:3],
        quote_currency=quote,
        resolved_at=resolved_at,
    )


def resolve_uic(search: Any, *, symbol: str) -> int:
    """The Uic for ``symbol`` from a ``/ref/v1/instruments`` keyword search.

    Matched on an exact, case-insensitive ``Symbol`` **and** ``AssetType == FxSpot``.
    A keyword search returns near matches, and "the first result" is how a watchlist
    quietly starts analysing a different instrument than the one it names.
    """
    data = search.get("Data") if isinstance(search, dict) else None
    if not isinstance(data, list):
        raise InstrumentUnresolved(f"{symbol}: instrument search returned no Data array")
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("AssetType") != "FxSpot":
            continue
        if str(entry.get("Symbol", "")).upper() != symbol.upper():
            continue
        uic = entry.get("Identifier", entry.get("Uic"))
        if isinstance(uic, int):
            return uic
        raise InstrumentUnresolved(f"{symbol}: search result carries no integer Uic")
    raise InstrumentUnresolved(f"{symbol}: no FxSpot instrument with this exact symbol")


def _require_int(value: Any, *, field: str, symbol: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstrumentUnresolved(f"{symbol}: {field} is {value!r}, expected an integer")
    return value


def _require_decimal(value: Any, *, field: str, symbol: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise InstrumentUnresolved(f"{symbol}: {field} is {value!r}, expected a number")
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise InstrumentUnresolved(f"{symbol}: {field} is {value!r}, expected a number")


__all__ = [
    "TICKS_PER_PIP",
    "ForexInstrument",
    "assert_pip",
    "instrument_from_details",
    "pip_from_decimals",
    "resolve_uic",
]
