"""What the analyst actually sees: selection, limits, and stable bytes."""

from __future__ import annotations

import json

from sentinel.analyst.serialization import analyst_payload, render_payload
from sentinel.core.config import AppConfig
from sentinel.ingestion.models import DataQuality, MarketSnapshot


def test_candles_are_not_in_the_payload(
    snapshot_with_features: MarketSnapshot, app_config: AppConfig
) -> None:
    """The three charts already show the series; ~1,200 candles of JSON would not
    add information, only tokens."""
    payload = analyst_payload(snapshot_with_features, app_config.risk)
    assert "ohlcv" not in payload
    # The feature block legitimately says `candles_used`; what must be absent is
    # the series itself -- an OHLC row for every bar.
    assert '"open_time"' not in json.dumps(payload)
    assert len(json.dumps(payload)) < 20_000, "payload has grown a series-sized limb"


def test_features_and_context_survive(
    snapshot_with_features: MarketSnapshot, app_config: AppConfig
) -> None:
    """Everything a chart cannot show has to be in the text."""
    payload = analyst_payload(snapshot_with_features, app_config.risk)
    assert payload["features"] is not None
    assert payload["symbol"] == "BTCUSDT"
    assert payload["last_price"]
    assert payload["data_quality"] == "OK"


def test_degradation_is_named_not_hidden(snapshot: MarketSnapshot, app_config: AppConfig) -> None:
    """PROMPTS §2 boundary 2 asks the analyst to be stricter on degraded data --
    it can only do that if it is told which fields degraded."""
    degraded = snapshot.model_copy(
        update={
            "data_quality": DataQuality.DEGRADED,
            "degraded_fields": ("funding", "news"),
        }
    )
    payload = analyst_payload(degraded, app_config.risk)
    assert payload["data_quality"] == "DEGRADED"
    assert payload["degraded_fields"] == ["funding", "news"]


def test_gate_limits_are_present(snapshot: MarketSnapshot, app_config: AppConfig) -> None:
    """specs/PROMPTS.md §2 "Inputs": the analyst is told what will be checked."""
    limits = analyst_payload(snapshot, app_config.risk)["gate_limits"]
    assert limits["min_rr_to_tp1"] == "1.5"
    assert limits["max_entry_distance_pct"] == "3.0"
    assert limits["min_confidence_for_candidate"] == 60


def test_no_sizing_information_reaches_the_analyst(
    snapshot_with_features: MarketSnapshot, app_config: AppConfig
) -> None:
    """ARCHITECTURE.md §4 / CLAUDE.md: the LLM never sizes and never sees EUR."""
    body = json.dumps(analyst_payload(snapshot_with_features, app_config.risk)).lower()
    for forbidden in ("capital_eur", "risk_per_trade", "leverage", "margin", "notional_eur"):
        assert forbidden not in body


def test_rendering_is_byte_stable(
    snapshot_with_features: MarketSnapshot, app_config: AppConfig
) -> None:
    """A prefix that moves between calls defeats prompt caching and snapshot tests."""
    payload = analyst_payload(snapshot_with_features, app_config.risk)
    assert render_payload(payload) == render_payload(dict(reversed(list(payload.items()))))


def test_decimals_render_as_strings_not_floats(
    snapshot: MarketSnapshot, app_config: AppConfig
) -> None:
    """Money never round-trips through float, not even into a prompt (CLAUDE.md)."""
    text = render_payload(analyst_payload(snapshot, app_config.risk))
    assert json.loads(text)["last_price"] == str(snapshot.last_price)
