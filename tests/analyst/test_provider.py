"""The analyst provider: schema validation, the retry rule, and refusals."""

from __future__ import annotations

import base64
import json

import pytest

from sentinel.analyst.models import CandidateStatus, Direction
from sentinel.analyst.providers.anthropic_fable import AnthropicFableAnalyst
from sentinel.analyst.providers.protocol import AnalystProvider
from sentinel.charts.models import ChartImage
from sentinel.core.config import AppConfig
from sentinel.ingestion.models import MarketSnapshot
from sentinel.llm.client import AnthropicClient
from sentinel.llm.errors import AnalystUnavailable
from sentinel.llm.models import LLMCallKind, LLMCallStatus
from tests.anthropic_double import ClientFactory, Recorder, error_response, message_payload
from tests.market_double import FAKE_PNG, valid_report_json

pytestmark = pytest.mark.asyncio

HISTORY = "RECENT PIPELINE HISTORY\n(none)"


def build(client: AnthropicClient, config: AppConfig) -> AnthropicFableAnalyst:
    return AnthropicFableAnalyst(client, config)


async def test_valid_response_becomes_a_stamped_report(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    client = client_factory([message_payload(valid_report_json())])
    analyst = build(client, app_config)

    report = await analyst.analyze(snapshot, charts, HISTORY)

    assert report.symbol == "BTCUSDT"
    assert report.candidate_status is CandidateStatus.CANDIDATE
    assert report.direction is Direction.LONG
    # Provenance is stamped by us, never taken from the model.
    assert report.prompt_version == "fable_v1"
    assert report.model == "claude-fable-5"
    assert len(analyst.calls) == 1
    assert analyst.calls[0].kind is LLMCallKind.ANALYST


async def test_prices_arrive_as_exact_decimals(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    """JSON numbers must reach money math through str(), not float (CLAUDE.md)."""
    from decimal import Decimal

    client = client_factory([message_payload(valid_report_json(stop=63400.1))])
    report = await build(client, app_config).analyze(snapshot, charts, HISTORY)

    assert report.stop == Decimal("63400.1")
    assert isinstance(report.stop, Decimal)


# ── charts and vision ───────────────────────────────────────────────────────


async def test_charts_are_ordered_context_setup_timing(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    """specs/PROMPTS.md §2 reasons 4h -> 1h -> 15m; the album renders ascending."""
    client = client_factory([message_payload(valid_report_json())])
    await build(client, app_config).analyze(snapshot, charts, HISTORY)

    blocks = recorder.last["messages"][0]["content"]
    captions = [b["text"] for b in blocks if b["type"] == "text" and b["text"].startswith("Chart")]
    assert [c.split()[3] for c in captions] == ["4h,", "1h,", "15m,"]


async def test_images_are_sent_as_base64_png(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    client = client_factory([message_payload(valid_report_json())])
    await build(client, app_config).analyze(snapshot, charts, HISTORY)

    images = [b for b in recorder.last["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 3
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(images[0]["source"]["data"]) == FAKE_PNG


async def test_missing_chart_is_logged_not_fatal(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    """PROMPTS §2 boundary 2: degrade and be stricter, never fabricate the input."""
    import structlog

    client = client_factory([message_payload(valid_report_json())])
    with structlog.testing.capture_logs() as logs:
        report = await build(client, app_config).analyze(snapshot, charts[:2], HISTORY)

    assert report.symbol == "BTCUSDT"
    assert any(line["event"] == "analyst.charts_missing" for line in logs)


async def test_request_audit_stores_image_references_not_bytes(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    """Owner ruling: sha256 + params, never ~400KB of PNG per call."""
    client = client_factory([message_payload(valid_report_json())])
    analyst = build(client, app_config)
    await analyst.analyze(snapshot, charts, HISTORY)

    audit = analyst.calls[0].request
    assert len(audit["images"]) == 3
    assert set(audit["images"][0]) == {"sha256", "params"}
    assert base64.standard_b64encode(FAKE_PNG).decode() not in json.dumps(audit)


# ── the retry rule (specs/PROMPTS.md §2, llm.max_json_retries) ──────────────


async def test_invalid_then_valid_succeeds_on_the_retry(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    client = client_factory(
        [message_payload("this is not json"), message_payload(valid_report_json())]
    )
    analyst = build(client, app_config)

    report = await analyst.analyze(snapshot, charts, HISTORY)

    assert report.candidate_status is CandidateStatus.CANDIDATE
    assert len(recorder) == 2
    assert [call.attempt for call in analyst.calls] == [1, 2]
    assert analyst.calls[0].status is LLMCallStatus.INVALID_JSON
    assert analyst.calls[1].status is LLMCallStatus.OK


async def test_retry_feeds_the_validation_error_back(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    """The model cannot fix what it is not shown."""
    client = client_factory(
        [message_payload('{"symbol": "BTCUSDT"}'), message_payload(valid_report_json())]
    )
    await build(client, app_config).analyze(snapshot, charts, HISTORY)

    retry_messages = recorder.requests[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert retry_messages[-1]["role"] == "user"
    assert "did not satisfy the required schema" in retry_messages[-1]["content"]
    assert "schema error(s)" in retry_messages[-1]["content"]


async def test_two_failures_discard_and_raise(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    """Retry once, then discard + log. Never guess (ARCHITECTURE.md §6)."""
    client = client_factory([message_payload("still not json")])
    analyst = build(client, app_config)

    with pytest.raises(AnalystUnavailable) as excinfo:
        await analyst.analyze(snapshot, charts, HISTORY)

    assert excinfo.value.reason == "invalid_json"
    assert len(recorder) == 2, "exactly one retry, per llm.max_json_retries=1"
    assert len(analyst.calls) == 2
    assert all(call.status is LLMCallStatus.INVALID_JSON for call in analyst.calls)


async def test_report_for_the_wrong_symbol_is_rejected(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    """A mislabelled report must never be routed onto another instrument."""
    client = client_factory([message_payload(valid_report_json(symbol="ETHUSDT"))])
    analyst = build(client, app_config)

    with pytest.raises(AnalystUnavailable) as excinfo:
        await analyst.analyze(snapshot, charts, HISTORY)

    assert "'ETHUSDT'" in (analyst.calls[0].error or "")
    assert excinfo.value.reason == "invalid_json"


async def test_out_of_range_confidence_is_rejected_client_side(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    """Structured outputs cannot express `le=100`; this model is the only guard."""
    client = client_factory([message_payload(valid_report_json(confidence=250))])
    analyst = build(client, app_config)

    with pytest.raises(AnalystUnavailable):
        await analyst.analyze(snapshot, charts, HISTORY)
    assert "confidence" in (analyst.calls[0].error or "")


async def test_truncated_response_is_not_retried_into_the_same_wall(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    client = client_factory([message_payload('{"symbol": "BTC', stop_reason="max_tokens")])
    analyst = build(client, app_config)

    with pytest.raises(AnalystUnavailable):
        await analyst.analyze(snapshot, charts, HISTORY)
    assert "truncated" in (analyst.calls[0].error or "")


# ── refusal and outage (no fallback, by owner ruling) ───────────────────────


async def test_refusal_discards_without_a_second_call(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
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
    analyst = build(client, app_config)

    with pytest.raises(AnalystUnavailable) as excinfo:
        await analyst.analyze(snapshot, charts, HISTORY)

    assert excinfo.value.reason == "refusal"
    assert len(recorder) == 1, "a refusal is not a schema problem; do not retry it"
    assert analyst.calls[0].refusal_category == "cyber"


async def test_api_error_is_recorded_then_raised(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    client = client_factory([error_response(500)])
    analyst = build(client, app_config)

    with pytest.raises(AnalystUnavailable) as excinfo:
        await analyst.analyze(snapshot, charts, HISTORY)

    assert excinfo.value.reason == "api_error"
    assert len(analyst.calls) == 1, "the failed call is still an audit row (PRD F10)"


# ── the seam M10 depends on (specs/ENSEMBLE.md §2) ──────────────────────────


async def test_provider_satisfies_the_protocol(
    client_factory: ClientFactory,
    app_config: AppConfig,
) -> None:
    client = client_factory([message_payload(valid_report_json())])
    provider: AnalystProvider = build(client, app_config)

    assert isinstance(provider, AnalystProvider)
    assert provider.name == "fable5"
    assert provider.prompt_version == "fable_v1"


async def test_analyze_signature_matches_the_spec(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
) -> None:
    """ENSEMBLE §2: analyze(snapshot, charts, history_block) -- positional, unchanged."""
    client = client_factory([message_payload(valid_report_json())])
    report = await build(client, app_config).analyze(snapshot, charts, HISTORY)
    assert report is not None


# --------------------------------------------------------------------------- #
# Empty text blocks are an HTTP 400, not an empty section (M10d, join 4)
# --------------------------------------------------------------------------- #


async def test_an_empty_history_block_is_omitted_rather_than_sent_empty(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    """The defect join 4 found, and the reason it could not be found any other way.

    The Messages API rejects an empty text block outright — ``messages: text content
    blocks must be non-empty``, HTTP 400 — so a section with nothing in it is not an
    empty section, it is a failed call. The forex path passed ``""`` as its history
    block, which means **every** forex analyst call would have died as an
    ``AnalystUnavailable`` that reads in the logs like an API problem rather than like a
    bug. Nothing caught it because the forex prompt had never once been sent, and every
    test in this file scripts the transport.

    Verified against the live endpoint before this was written: the same payload with
    one empty text block 400s, and without it counts 8 tokens.
    """
    client = client_factory([message_payload(valid_report_json())])
    await build(client, app_config).analyze(snapshot, charts, "")

    blocks = recorder.last["messages"][0]["content"]
    empty = [b for b in blocks if b["type"] == "text" and not b["text"].strip()]
    assert empty == [], f"{len(empty)} empty text block(s) would 400 the whole call"


async def test_a_real_history_block_is_still_sent_whole(
    client_factory: ClientFactory,
    app_config: AppConfig,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    recorder: Recorder,
) -> None:
    """The sibling that keeps the fix above from becoming "history is optional".

    Dropping empty blocks must not drop a *present* one. specs/PROMPTS.md §3's
    calibration block is the analyst's only view of its own measured performance, and
    losing it silently would be a worse defect than the 400 — the call would succeed
    and the reasoning would be poorer with nothing to notice it by.
    """
    client = client_factory([message_payload(valid_report_json())])
    await build(client, app_config).analyze(snapshot, charts, HISTORY)

    texts = [b["text"] for b in recorder.last["messages"][0]["content"] if b["type"] == "text"]
    assert HISTORY in texts
