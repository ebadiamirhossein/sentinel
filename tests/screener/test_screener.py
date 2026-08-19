"""Batch triage: reconciliation, retry, and what happens when it all fails."""

from __future__ import annotations

import json

import pytest

from sentinel.core.config import AppConfig
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import MarketSnapshot
from sentinel.llm.models import LLMCallKind, LLMCallStatus
from sentinel.screener.screener import Screener
from tests.anthropic_double import ClientFactory, Recorder, error_response, message_payload

pytestmark = pytest.mark.asyncio


def batch(*verdicts: dict[str, object]) -> str:
    return json.dumps({"verdicts": list(verdicts)})


def verdict_json(symbol: str, interesting: bool = False, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": symbol,
        "interesting": interesting,
        "direction_hint": "long" if interesting else "unclear",
        "reason": "testing",
    }
    payload.update(overrides)
    return payload


def two_symbols(
    snapshot: MarketSnapshot, features: SymbolFeatures
) -> tuple[list[MarketSnapshot], dict[str, SymbolFeatures]]:
    """BTCUSDT plus a copy relabelled ETHUSDT -- enough to test reconciliation."""
    other = snapshot.model_copy(update={"symbol": "ETHUSDT"})
    other_features = features.model_copy(update={"symbol": "ETHUSDT"})
    return [snapshot, other], {"BTCUSDT": features, "ETHUSDT": other_features}


# ── the batch call ─────────────────────────────────────────────────────────


async def test_batch_returns_one_verdict_per_symbol(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    snapshots, feature_map = two_symbols(snapshot, features)
    client = client_factory(
        [message_payload(batch(verdict_json("BTCUSDT", True), verdict_json("ETHUSDT")))]
    )

    result = await Screener(client, app_config).screen(snapshots, feature_map)

    assert len(result.verdicts) == 2
    assert len(result.interesting) == 1
    assert result.degraded is False
    assert len(recorder) == 1, "one batch call, not one per symbol"


async def test_batch_sends_the_triage_block_not_free_text(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    client = client_factory([message_payload(batch(verdict_json("BTCUSDT")))])
    await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    body = recorder.last["messages"][0]["content"]
    assert "news_flag" in body
    assert "Return exactly one verdict per symbol" in body
    assert recorder.last["model"] == "claude-sonnet-4-6"
    # The cheap tier gets no effort hint -- it is triage, not reasoning.
    assert "effort" not in recorder.last.get("output_config", {})


async def test_screener_call_is_recorded_without_a_symbol(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
) -> None:
    """The screener is batch-level, so the audit row's symbol is null by design."""
    client = client_factory([message_payload(batch(verdict_json("BTCUSDT")))])
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    assert result.calls[0].symbol is None
    assert result.calls[0].kind is LLMCallKind.SCREENER
    # Whatever config selects — asserting the *stored* version tracks the config
    # rather than a constant, which is what /stats groups by (M8.2).
    assert result.calls[0].prompt_version == app_config.llm.screener_prompt_version


async def test_symbols_without_features_are_skipped(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    """M2 could not compute features -> there is nothing to triage on."""
    snapshots, _ = two_symbols(snapshot, features)
    client = client_factory([message_payload(batch(verdict_json("BTCUSDT")))])

    result = await Screener(client, app_config).screen(snapshots, {"BTCUSDT": features})

    assert [v.symbol for v in result.verdicts] == ["BTCUSDT"]


async def test_no_usable_snapshots_makes_no_call(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    recorder: Recorder,
) -> None:
    client = client_factory([message_payload(batch())])
    result = await Screener(client, app_config).screen([snapshot], {})

    assert result.verdicts == ()
    assert len(recorder) == 0


async def test_batching_splits_when_configured(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    snapshots, feature_map = two_symbols(snapshot, features)
    config = app_config.model_copy(
        update={"llm": app_config.llm.model_copy(update={"screener_batch_size": 1})}
    )
    client = client_factory(
        [
            message_payload(batch(verdict_json("BTCUSDT"))),
            message_payload(batch(verdict_json("ETHUSDT"))),
        ]
    )

    result = await Screener(client, config).screen(snapshots, feature_map)

    assert len(recorder) == 2
    assert [v.symbol for v in result.verdicts] == ["BTCUSDT", "ETHUSDT"]


# ── failure: never fabricate a candidate ───────────────────────────────────


async def test_invalid_then_valid_succeeds_on_the_retry(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    client = client_factory(
        [message_payload("not json"), message_payload(batch(verdict_json("BTCUSDT", True)))]
    )
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    assert len(result.interesting) == 1
    assert len(recorder) == 2
    assert [call.attempt for call in result.calls] == [1, 2]
    assert result.calls[0].status is LLMCallStatus.INVALID_JSON


async def test_two_failures_yield_zero_candidates_not_a_guess(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    """An empty cycle is correct; a fabricated candidate costs a deep call."""
    client = client_factory([message_payload("still not json")])
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    assert result.interesting == ()
    assert result.degraded is True
    assert result.verdicts[0].reason == "screener unavailable this cycle"
    assert len(recorder) == 2


async def test_api_error_is_not_retried_as_a_schema_problem(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    recorder: Recorder,
) -> None:
    client = client_factory([error_response(500)])
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    assert result.degraded is True
    assert result.interesting == ()
    # 3 transport attempts inside one logical call, then we stop -- no schema retry.
    assert len(result.calls) == 1


async def test_refusal_degrades_the_batch(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
) -> None:
    client = client_factory(
        [
            message_payload(
                "",
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "no"},
            )
        ]
    )
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    assert result.degraded is True
    assert result.calls[0].status is LLMCallStatus.REFUSAL


async def test_screener_never_raises(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
) -> None:
    """A failed screener must not take the cycle down with it."""
    client = client_factory([error_response(503)])
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})
    assert isinstance(result.verdicts, tuple)


async def test_over_long_reason_is_a_schema_failure(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
) -> None:
    """specs/PROMPTS.md §1 caps the reason at 200 chars; that bound is ours to hold."""
    client = client_factory([message_payload(batch(verdict_json("BTCUSDT", reason="x" * 500)))])
    result = await Screener(client, app_config).screen([snapshot], {"BTCUSDT": features})

    assert result.degraded is True
    assert "schema error" in (result.calls[0].error or "")
