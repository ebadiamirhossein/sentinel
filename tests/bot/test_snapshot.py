"""``/snapshot`` — the deterministic view, and what it must say when it cannot (M8.6).

The counterpart to ``test_pulse.py``. That command shows what a model said; this one
shows the numbers the model was given, and the two failure modes are opposites:
``/pulse`` must not leak somebody's account, and ``/snapshot`` — which has nothing
per-user to leak — must not quietly render a missing measurement as a present one.

So the tests here are mostly about **absence**:

* every block of the snapshot missing, one at a time, and named in words rather than
  dropped or zero-filled. A funding rate that was not fetched is not a funding rate
  of zero, and telling those two apart is half of why this command exists;
* a stored block that no longer validates costing one section and not the card;
* the two distinct "nothing stored" answers, because on-the-watchlist-and-not-yet
  and not-on-the-watchlist-at-all are different problems with different fixes.

The snapshot is built from the **recorded cassettes and the real feature engine**,
not from a hand-written dict: the whole point of the card is that it shows what the
pipeline actually computed, and a fixture that skipped the pipeline would prove
nothing about the shapes it stores.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.cards import snapshot_card
from sentinel.bot.snapshot import MAX_LEVELS, NA, TIMEFRAMES, snapshot_view
from sentinel.bot.views import SnapshotView
from sentinel.core.config import AppConfig
from sentinel.features.engine import attach, compute
from sentinel.ingestion.models import (
    BookSnapshot,
    DerivContext,
    MacroContext,
    MarketSnapshot,
    OpenInterestPoint,
    SentimentContext,
)
from sentinel.storage.models import MarketSnapshotRow
from sentinel.storage.repositories import snapshot_context
from tests.bot.telegram_html import assert_sendable, unsupported
from tests.market_double import snapshot_from_cassettes

TZ = ZoneInfo("Europe/Vilnius")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: The four sections that are ``None`` when a snapshot did not carry them, and the
#: phrase each renders instead. Parametrized rather than written out four times: the
#: property under test is "every one of them says so", and a fifth block added
#: without a sentence would be a silent gap on a card whose job is completeness.
OPTIONAL_BLOCKS = {
    "derivatives": "Derivatives",
    "orderbook": "Order book",
    "sentiment": "Fear &amp; Greed",
    "macro": "BTC dominance",
}


def derivatives() -> DerivContext:
    return DerivContext(
        source="binance_usdm",
        fetched_at=NOW,
        funding_rate=Decimal("0.0000193"),
        next_funding_time=NOW + timedelta(hours=4),
        open_interest_base=Decimal("3412905.4"),
        open_interest_value=Decimal("284120500"),
        open_interest_24h=(
            OpenInterestPoint(at=NOW - timedelta(hours=24), open_interest_base=Decimal("3336000")),
            OpenInterestPoint(at=NOW, open_interest_base=Decimal("3412905.4")),
        ),
        long_short_ratio=Decimal("1.42"),
    )


def orderbook() -> BookSnapshot:
    return BookSnapshot(
        source="binance_usdm",
        fetched_at=NOW,
        depth_levels=20,
        best_bid=Decimal("83.39"),
        best_ask=Decimal("83.41"),
        spread_pct=Decimal("0.024"),
        bid_notional=Decimal("560000"),
        ask_notional=Decimal("440000"),
        imbalance=Decimal("0.12"),
    )


def full_snapshot(config: AppConfig, **overrides: Any) -> MarketSnapshot:
    """A cassette snapshot with real features and every optional block attached."""
    snapshot = snapshot_from_cassettes("BTCUSDT")
    snapshot = attach(snapshot, compute(snapshot, config.features))
    parts: dict[str, Any] = {
        "derivatives": derivatives(),
        "orderbook": orderbook(),
        "sentiment": SentimentContext(
            source="alternative.me",
            fetched_at=NOW,
            value=74,
            classification="Greed",
            previous_value=68,
        ),
        "macro": MacroContext(
            source="coingecko",
            fetched_at=NOW,
            btc_dominance_pct=Decimal("54.2"),
            total_mcap_change_24h_pct=Decimal("1.1"),
        ),
    }
    parts.update(overrides)
    return snapshot.model_copy(update=parts)


def row_of(snapshot: MarketSnapshot, **overrides: Any) -> MarketSnapshotRow:
    """The ORM row as ``SnapshotRepository.save`` writes it — real serializer, real
    column names, so a renamed field fails here rather than on a phone."""
    values: dict[str, Any] = {
        "symbol": snapshot.symbol,
        "captured_at": snapshot.captured_at,
        "last_price": snapshot.last_price,
        "data_quality": snapshot.data_quality.value,
        "degraded_fields": list(snapshot.degraded_fields),
        "context": snapshot_context(snapshot),
    }
    values.update(overrides)
    return MarketSnapshotRow(**values)


@pytest.fixture
def view(bot_config: AppConfig) -> SnapshotView:
    return snapshot_view(row_of(full_snapshot(bot_config)), symbol="BTCUSDT", on_watchlist=True)


@pytest.fixture
def card(view: SnapshotView) -> str:
    return snapshot_card(view, TZ)


# --------------------------------------------------------------------------- #
# Everything the owner asked to see is on the card
# --------------------------------------------------------------------------- #


def test_the_card_says_it_contains_no_ai_output(card: str) -> None:
    """The label is the product. A wall of indicators that a reader might take for
    an opinion is worse than no card, and the place the opinion lives is named."""
    assert "measured, not analysed" in card
    assert "before any AI analysis" in card
    assert "/pulse BTCUSDT" in card


def test_every_requested_timeframe_has_a_row(view: SnapshotView) -> None:
    assert [tf.timeframe for tf in view.timeframes] == list(TIMEFRAMES)


def test_each_timeframe_carries_the_indicators_and_the_basis_behind_its_regime(
    view: SnapshotView, card: str
) -> None:
    """``regime_basis`` rides beside the regime rather than being dropped when it is
    ``FULL``. A 1d tail of 100 candles has no EMA200 and is classified ``REDUCED``;
    a card showing only the verdict would let a reduced read pass for a full one,
    which is the distinction ``RegimeBasis`` exists to preserve."""
    hourly = next(tf for tf in view.timeframes if tf.timeframe == "1h")

    assert hourly.ema20 != NA and hourly.ema50 != NA
    assert hourly.rsi14 != NA and hourly.atr14 != NA and hourly.atr_pct != NA
    assert hourly.relative_volume != NA
    for value in (hourly.regime, hourly.volatility, hourly.regime_basis.lower(), hourly.ema20):
        assert value in card


def test_the_daily_reduced_read_is_shown_as_reduced(view: SnapshotView, card: str) -> None:
    """The cassette's 1d series is 100 candles, so EMA200 genuinely cannot exist.
    This is the real degradation the feature engine records, not a contrived one."""
    daily = next(tf for tf in view.timeframes if tf.timeframe == "1d")

    assert daily.ema200 == NA
    assert daily.regime_basis == "REDUCED"
    assert "reduced" in card


def test_levels_carry_their_touch_counts(view: SnapshotView, card: str) -> None:
    """The touch count is the point: a zone touched five times is structure and one
    touched once is a coincidence. The analyst is given both, so showing the number
    is what lets a reader judge its use of them."""
    assert view.levels, "the cassette should cluster at least one level"
    assert all(level.touches >= 1 for level in view.levels)
    assert "touches" in card
    assert "touch(es)" not in card, "a card that says '1 touch(es)' erodes trust in the rest"


def test_levels_are_capped_and_the_card_says_how_many_it_dropped(bot_config: AppConfig) -> None:
    """M8.4 decision 6. A bounded list that does not say it is bounded reads as
    "that was everything", which on this card would be the one lie it must not
    tell."""
    snapshot = full_snapshot(bot_config)
    features = compute(snapshot, bot_config.features)
    many = features.model_copy(update={"levels": tuple(features.levels) * 12})
    row = row_of(attach(snapshot, many))

    view = snapshot_view(row, symbol="BTCUSDT", on_watchlist=True)
    assert len(view.levels) == MAX_LEVELS
    assert view.levels_dropped > 0
    assert f"+{view.levels_dropped} further out" in snapshot_card(view, TZ)


def test_the_funding_rate_is_shown_as_a_percentage_with_enough_decimals(
    view: SnapshotView,
) -> None:
    """Stored as a fraction; 0.0000193 is 0.00193%. Two decimal places would render
    every normal funding rate as ``0.00%`` — a figure that looks measured and says
    nothing."""
    assert view.derivatives is not None
    assert view.derivatives.funding_pct == "+0.0019%"


def test_open_interest_reports_its_own_24h_change(view: SnapshotView, card: str) -> None:
    """The one derivation in the module: ``DerivContext`` stores the series and no
    delta, because nothing upstream ever needed one."""
    assert view.derivatives is not None
    assert view.derivatives.change_24h_pct == "+2.31%"
    assert "24h change" in card


def test_a_single_open_interest_reading_is_not_a_change(bot_config: AppConfig) -> None:
    """One point is a reading. Reporting 0% would claim open interest held flat for
    a day, which is a measurement nobody made."""
    lone = derivatives().model_copy(
        update={"open_interest_24h": (OpenInterestPoint(at=NOW, open_interest_base=Decimal("1")),)}
    )
    row = row_of(full_snapshot(bot_config, derivatives=lone))

    view = snapshot_view(row, symbol="BTCUSDT", on_watchlist=True)
    assert view.derivatives is not None
    assert view.derivatives.change_24h_pct == NA


def test_the_open_interest_notional_is_omitted_when_the_exchange_did_not_publish_it(
    bot_config: AppConfig,
) -> None:
    """Found on a live ADAUSDT row, never on a fixture: ``open_interest_value`` is
    optional on ``DerivContext`` and Binance often omits it, so the card was reading
    "Open interest 7,696,615 in base units (≈ n/a USDT)". No parenthetical is a
    better line than an empty one."""
    without = derivatives().model_copy(update={"open_interest_value": None})
    row = row_of(full_snapshot(bot_config, derivatives=without))
    card = snapshot_card(snapshot_view(row, symbol="BTCUSDT", on_watchlist=True), TZ)

    line = next(row for row in card.splitlines() if "Open interest" in row)
    assert line.endswith("in base units")
    assert "n/a" not in line and "≈" not in line


def test_the_market_context_block_is_there(view: SnapshotView, card: str) -> None:
    assert view.sentiment is not None and view.sentiment.value == 74
    assert view.sentiment.delta == "+6", "yesterday's delta is what makes the number readable"
    assert view.macro is not None
    assert "Fear &amp; Greed 74 (Greed)" in card
    assert "BTC dominance 54.20%" in card


def test_the_order_book_imbalance_is_on_the_card(view: SnapshotView, card: str) -> None:
    assert view.book is not None
    assert "Imbalance" in card and view.book.imbalance in card


def test_data_quality_names_the_fields_that_degraded(bot_config: AppConfig) -> None:
    """ "DEGRADED" alone says something is wrong and not what. The whole list is on
    the row, so it is printed — this is the line that says whether the analyst was
    working with everything."""
    snapshot = snapshot_from_cassettes("BTCUSDT", degraded=True)
    snapshot = attach(snapshot, compute(snapshot, bot_config.features))
    view = snapshot_view(row_of(snapshot), symbol="BTCUSDT", on_watchlist=True)

    card = snapshot_card(view, TZ)
    assert "DEGRADED" in card
    assert "funding" in card and "news" in card


# --------------------------------------------------------------------------- #
# Absence — the half of this card that is about what could not be measured
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("block", "heading"), sorted(OPTIONAL_BLOCKS.items()))
def test_a_missing_block_is_named_rather_than_dropped(
    bot_config: AppConfig, block: str, heading: str
) -> None:
    """CLAUDE.md's degrade-explicitly rule, one block at a time.

    A section that simply vanished would leave a reader unable to tell "the pipeline
    did not fetch this" from "this card does not show it" — and the first of those
    is a fact about the run that ``/snapshot`` exists to surface.
    """
    row = row_of(full_snapshot(bot_config, **{block: None}))
    card = snapshot_card(snapshot_view(row, symbol="BTCUSDT", on_watchlist=True), TZ)

    assert heading in card
    assert "not recorded on this snapshot" in card


def test_a_missing_block_is_never_rendered_as_zero(bot_config: AppConfig) -> None:
    """The specific failure the rule above exists to prevent: a funding rate that was
    not fetched is not a funding rate of zero, and a card that showed 0.0000% would
    be inventing a market fact."""
    row = row_of(full_snapshot(bot_config, derivatives=None))
    view = snapshot_view(row, symbol="BTCUSDT", on_watchlist=True)

    assert view.derivatives is None
    assert "0.0000%" not in snapshot_card(view, TZ)


def test_a_feature_block_that_will_not_validate_costs_the_indicators_and_not_the_card(
    bot_config: AppConfig,
) -> None:
    """M8.5 decision 4's posture, one table over. Price, capture time and data
    quality are the row's own columns and survive a schema change; the indicators
    live in JSONB and do not. The card reports which happened rather than rendering
    an empty table that reads as a quiet market."""
    snapshot = full_snapshot(bot_config)
    context = snapshot_context(snapshot)
    context["features"] = {"schema_version": 99, "not": "a feature block"}
    view = snapshot_view(row_of(snapshot, context=context), symbol="BTCUSDT", on_watchlist=True)

    assert view.features_unreadable is True
    assert view.timeframes == () and view.levels == ()
    assert view.last_price != "" and view.at is not None

    card = snapshot_card(view, TZ)
    assert "does not match this build" in card
    assert "Derivatives" in card, "one bad block must not cost the rest of the card"


def test_a_snapshot_with_no_features_at_all_is_not_reported_as_unreadable(
    bot_config: AppConfig,
) -> None:
    """Never computed and computed-but-unreadable are different facts. Only the
    second gets the warning; the first is what an ingestion-only row looks like."""
    snapshot = snapshot_from_cassettes("BTCUSDT")
    view = snapshot_view(row_of(snapshot), symbol="BTCUSDT", on_watchlist=True)

    assert view.features_unreadable is False
    assert "does not match this build" not in snapshot_card(view, TZ)


def test_a_symbol_with_nothing_stored_gets_the_answer_that_fits_its_case() -> None:
    """Two nothings, two fixes — the same split ``/pulse SOLUSDT`` makes. On the
    watchlist and not yet ingested clears itself; off the watchlist means nothing is
    looking at it, and the command to change that differs by role."""
    watched = snapshot_card(snapshot_view(None, symbol="SOLUSDT", on_watchlist=True), TZ)
    unwatched = snapshot_card(snapshot_view(None, symbol="SOLUSDT", on_watchlist=False), TZ)

    assert "On the watchlist" in watched and "/pulse" in watched
    assert "Not on the watchlist" in unwatched
    assert "/request SOLUSDT" in unwatched and "/watchlist add SOLUSDT" in unwatched


# --------------------------------------------------------------------------- #
# The boundaries
# --------------------------------------------------------------------------- #


def test_the_view_has_no_field_that_could_carry_per_user_information() -> None:
    """Structural, not a renderer's restraint — ``UserView``'s mechanism (M8.1 §6)
    and ``PulseView.spend``'s (M8.4 §5).

    ``/snapshot`` is shared market data and there is nothing on a
    ``market_snapshots`` row that could name a person, so this is cheap today. It is
    written down because the next person to widen this card will be reaching for
    something convenient, and "the type has no field for it" is the only version of
    this rule that survives that.
    """
    forbidden = ("user", "chat", "capital", "risk_eur", "decision", "pnl", "size")
    fields = set(SnapshotView.__dataclass_fields__)

    assert not [name for name in fields for word in forbidden if word in name]


def test_the_same_snapshot_renders_identically_for_every_reader(view: SnapshotView) -> None:
    """The design claim stated outright: unlike ``/pulse``, this card has no spend
    line and therefore nothing at all that varies by role. There is one rendering
    path and no role is passed to it — the handler does not even take an ``Actor``.
    """
    import inspect

    from sentinel.bot.handlers.commands import snapshot as handler

    assert "actor" not in inspect.signature(handler).parameters
    assert snapshot_card(view, TZ) == snapshot_card(view, TZ)


def test_external_text_on_the_card_is_escaped(bot_config: AppConfig) -> None:
    """A classification string and a symbol both arrive from outside this system.
    M5 §4 established that external text reaching a card is attacker-influenceable,
    and escaping is done once at the boundary rather than trusted per field."""
    hostile = SentimentContext(
        source="alternative.me",
        fetched_at=NOW,
        value=74,
        classification="<script>alert(1)</script>",
        previous_value=68,
    )
    row = row_of(full_snapshot(bot_config, sentiment=hostile))
    card = snapshot_card(snapshot_view(row, symbol="BTCUSDT", on_watchlist=True), TZ)

    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_a_full_card_fits_one_telegram_message(bot_config: AppConfig) -> None:
    """4096 is a refusal, not a truncation: an over-long card is not delivered at
    all. Filled to the level cap with every optional block present, which is the
    largest this card can be — the timeframe count is fixed and the level list is
    the only thing that grows."""
    snapshot = full_snapshot(bot_config)
    features = compute(snapshot, bot_config.features)
    row = row_of(
        attach(snapshot, features.model_copy(update={"levels": tuple(features.levels) * 12}))
    )

    card = snapshot_card(snapshot_view(row, symbol="BTCUSDT", on_watchlist=True), TZ)
    assert len(card) < 4096, f"the snapshot card is {len(card)} characters"


def test_a_price_off_the_numeric_column_is_not_shown_with_eighteen_trailing_zeros() -> None:
    """Found by the Postgres round-trip, not by a fixture (M8.4 §9a's lesson again).

    ``market_snapshots.last_price`` is ``Numeric(38, 18)``, so a price the fixtures
    hold as ``64100.0`` comes back out of the database as
    ``64100.000000000000000000``. Rendering that verbatim reads as a bug; rendering
    it through ``normalize()`` alone gives ``6.41E+4``, which reads as a worse one.

    Both directions are asserted, plus a small price, because the same helper serves
    a symbol quoted in the tens of thousands and one quoted to eight decimals.
    """
    from decimal import Decimal

    from sentinel.bot.snapshot import _sig

    assert _sig(Decimal("64100.000000000000000000")) == "64100"
    assert _sig(Decimal("0.000012340000000000")) == "0.00001234"
    assert _sig(Decimal("82.54714285714286")) == "82.5471"
    assert _sig(None) == NA


# --------------------------------------------------------------------------- #
# The production defect: a card Telegram refuses to parse (M8.6, 2026-08-20)
# --------------------------------------------------------------------------- #


def test_the_ema_stack_cannot_break_telegrams_parser(bot_config: AppConfig) -> None:
    """The regression. ``/snapshot ADAUSDT`` was silent for every symbol in
    production and the card was perfect locally.

    ``ema_stack`` is the feature engine's comparison chain — ``20>50<200`` — and it
    reached the card unescaped. Telegram read ``<200`` as an opening tag, refused the
    **whole message** with "Unsupported start tag", and aiogram logged the exception
    where nobody was looking. The caller got nothing.

    Both orderings are asserted because only one of them is fatal: ``>`` is tolerated
    as text and ``<`` is not, so a fixture that happened to produce a
    strictly-descending stack would have passed.
    """
    snapshot = full_snapshot(bot_config)
    features = compute(snapshot, bot_config.features)
    hourly = features.timeframes["1h"]
    rewritten = {
        "15m": features.timeframes["15m"].model_copy(update={"ema_stack": "20>50>200"}),
        "1h": hourly.model_copy(update={"ema_stack": "20>50<200"}),
        "4h": features.timeframes["4h"].model_copy(update={"ema_stack": "20<50<200"}),
        "1d": features.timeframes["1d"].model_copy(update={"ema_stack": "20<50"}),
    }
    row = row_of(attach(snapshot, features.model_copy(update={"timeframes": rewritten})))

    card = snapshot_card(snapshot_view(row, symbol="BTCUSDT", on_watchlist=True), TZ)

    assert_sendable(card, what="the snapshot card")
    assert "20&gt;50&lt;200" in card, "the stack must still be readable, only escaped"
    assert "<200" not in card


def test_every_shape_this_card_takes_is_something_telegram_will_send(
    bot_config: AppConfig,
) -> None:
    """The class, not the instance.

    One escaped field fixes one bug; this sweeps every state the card has — full,
    each block missing, features unreadable, both "nothing stored" answers — and
    asserts each is parseable. Telegram does not sanitize a message it cannot read,
    it **refuses** it, so an unescaped ``<`` anywhere on any of these is silence for
    the caller.
    """
    cases: dict[str, SnapshotView] = {
        "full": snapshot_view(
            row_of(full_snapshot(bot_config)), symbol="BTCUSDT", on_watchlist=True
        ),
        "nothing stored, watched": snapshot_view(None, symbol="SOLUSDT", on_watchlist=True),
        "nothing stored, unwatched": snapshot_view(None, symbol="SOLUSDT", on_watchlist=False),
    }
    for block in OPTIONAL_BLOCKS:
        cases[f"no {block}"] = snapshot_view(
            row_of(full_snapshot(bot_config, **{block: None})), symbol="BTCUSDT", on_watchlist=True
        )

    degraded = snapshot_from_cassettes("BTCUSDT", degraded=True)
    cases["degraded"] = snapshot_view(
        row_of(attach(degraded, compute(degraded, bot_config.features))),
        symbol="BTCUSDT",
        on_watchlist=True,
    )

    for name, view in cases.items():
        assert_sendable(snapshot_card(view, TZ), what=f"the snapshot card ({name})")


def test_the_sweep_would_catch_the_bug_that_shipped() -> None:
    """Proof of teeth, against the literal production line.

    Python's own ``html.parser`` treats ``<200`` as text — tag names cannot start
    with a digit — so reaching for the standard library here would have produced a
    validator that passed the broken card. This asserts the checker encodes
    *Telegram's* rule and not HTML's.
    """
    shipped = "  <b>1h</b> RANGE (full) · vol HIGH · stack 20>50<200"

    assert unsupported(shipped) == ["<200"]
    with pytest.raises(AssertionError, match="silence"):
        assert_sendable(shipped)
