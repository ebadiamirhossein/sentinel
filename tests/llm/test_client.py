"""The client wrapper: request shape, transport policy, cost logging, statuses."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from sentinel.core.config import AppConfig
from sentinel.llm.client import AnthropicClient, LLMResult
from sentinel.llm.models import LLMCallKind, LLMCallStatus, TokenUsage
from tests.anthropic_double import ClientFactory, Recorder, error_response, message_payload

pytestmark = pytest.mark.asyncio

SIMPLE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


async def complete(client: AnthropicClient, **overrides: object) -> LLMResult:
    kwargs: dict[str, object] = {
        "kind": LLMCallKind.ANALYST,
        "model": "claude-fable-5",
        "prompt_version": "fable_v1",
        "system": "SYSTEM PROMPT",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 4096,
        "timeout_seconds": 5.0,
        "effort": "high",
        "output_schema": SIMPLE_SCHEMA,
        "request_audit": {"system": "SYSTEM PROMPT", "text_blocks": ["hello"], "images": []},
        "symbol": "SOLUSDT",
    }
    kwargs.update(overrides)
    return await client.complete(**kwargs)  # type: ignore[arg-type]


# ── Fable 5 request contract ────────────────────────────────────────────────
# Each assertion here is a 400 on the live API if it regresses.


async def test_request_omits_thinking_and_sampling_params(
    client_factory: ClientFactory,
    recorder: Recorder,
) -> None:
    """`thinking` and temperature/top_p/top_k are all rejected on claude-fable-5."""
    client = client_factory([message_payload('{"ok": true}')])
    await complete(client)

    body = recorder.last
    assert "thinking" not in body, "claude-fable-5 rejects any thinking config; omit it entirely"
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body


async def test_request_carries_effort_and_schema(
    client_factory: ClientFactory,
    recorder: Recorder,
) -> None:
    client = client_factory([message_payload('{"ok": true}')])
    await complete(client)

    output_config = recorder.last["output_config"]
    assert output_config["effort"] == "high"
    assert output_config["format"] == {"type": "json_schema", "schema": SIMPLE_SCHEMA}


async def test_no_assistant_prefill(client_factory: ClientFactory, recorder: Recorder) -> None:
    """A trailing assistant turn is a 400 on this model family."""
    client = client_factory([message_payload('{"ok": true}')])
    await complete(client)

    assert recorder.last["messages"][-1]["role"] == "user"


async def test_system_prompt_is_a_cache_breakpoint(
    client_factory: ClientFactory,
    recorder: Recorder,
) -> None:
    """Stable prefix cached; volatile per-symbol content stays in `messages`."""
    client = client_factory([message_payload('{"ok": true}')])
    await complete(client)

    system = recorder.last["system"]
    assert system == [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
    ]


async def test_output_config_omitted_when_unused(
    client_factory: ClientFactory,
    recorder: Recorder,
) -> None:
    """Sending an empty output_config would be a pointless 400 risk."""
    client = client_factory([message_payload("plain text")])
    await complete(client, effort=None, output_schema=None)

    assert "output_config" not in recorder.last


# ── statuses ────────────────────────────────────────────────────────────────


async def test_success_records_tokens_cost_and_duration(
    client_factory: ClientFactory,
    app_config: AppConfig,
) -> None:
    client = client_factory(
        [message_payload('{"ok": true}', input_tokens=12000, output_tokens=1500)]
    )
    result = await complete(client)

    assert result.ok
    assert result.text() == '{"ok": true}'
    call = result.call
    assert call.usage == TokenUsage(input_tokens=12000, output_tokens=1500)
    # 12000 * $10/Mtok + 1500 * $50/Mtok = 0.12 + 0.075
    assert call.cost_usd_estimate == Decimal("0.195000")
    assert call.status is LLMCallStatus.OK
    assert call.request_id == "req_test"
    assert call.symbol == "SOLUSDT"
    assert call.attempt == 1


async def test_refusal_is_recorded_not_raised(client_factory: ClientFactory) -> None:
    """HTTP 200 + stop_reason=refusal. The record must survive it (PRD F10)."""
    client = client_factory(
        [
            message_payload(
                "",
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
            )
        ]
    )
    result = await complete(client)

    assert result.call.status is LLMCallStatus.REFUSAL
    assert result.call.refusal_category == "cyber"
    assert result.call.stop_reason == "refusal"


async def test_no_server_side_fallback_is_requested(
    client_factory: ClientFactory,
    recorder: Recorder,
) -> None:
    """Owner ruling: a refusal is discarded, never silently answered by another model."""
    client = client_factory([message_payload('{"ok": true}')])
    await complete(client)

    assert "fallbacks" not in recorder.last


async def test_truncation_is_its_own_status(client_factory: ClientFactory) -> None:
    """`max_tokens` truncation is "we gave it no room", not "the model is wrong"."""
    client = client_factory([message_payload('{"ok": tr', stop_reason="max_tokens")])
    result = await complete(client)

    assert result.call.status is LLMCallStatus.TRUNCATED
    assert "max_tokens=4096" in (result.call.error or "")


async def test_thinking_blocks_are_skipped_when_reading_text(client_factory: ClientFactory) -> None:
    """Fable 5 returns thinking blocks with empty text; only text blocks are content."""
    payload = message_payload('{"ok": true}')
    payload["content"] = [
        {"type": "thinking", "thinking": "", "signature": ""},
        {"type": "text", "text": '{"ok": true}'},
    ]
    client = client_factory([payload])
    result = await complete(client)

    assert result.text() == '{"ok": true}'


# ── transport policy (CLAUDE.md: timeout + max-2 retries + structured error) ──


async def test_retries_5xx_then_succeeds(client_factory: ClientFactory, recorder: Recorder) -> None:
    client = client_factory([error_response(500), message_payload('{"ok": true}')])
    result = await complete(client)

    assert result.ok
    assert len(recorder) == 2, "a 500 should be retried by the SDK"


async def test_does_not_retry_4xx(client_factory: ClientFactory, recorder: Recorder) -> None:
    """A 400 will not fix itself; hammering it wastes the cycle's time budget."""
    client = client_factory([error_response(400, "invalid_request_error")])
    result = await complete(client)

    assert len(recorder) == 1
    assert result.call.status is LLMCallStatus.API_ERROR
    assert "HTTP 400" in (result.call.error or "")
    assert result.message is None


async def test_retry_budget_is_bounded(client_factory: ClientFactory, recorder: Recorder) -> None:
    """max_transport_retries=2 means at most three attempts, then a recorded failure."""
    client = client_factory([error_response(500)])
    result = await complete(client)

    assert len(recorder) == 3
    assert result.call.status is LLMCallStatus.API_ERROR


async def test_timeout_is_recorded(app_config: AppConfig, recorder: Recorder) -> None:
    from tests.anthropic_double import make_client

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(json.loads(request.content.decode()))
        raise httpx.ReadTimeout("too slow", request=request)

    client = make_client(httpx.MockTransport(handler), app_config, max_retries=0)
    result = await complete(client)

    assert result.call.status is LLMCallStatus.TIMEOUT
    assert result.message is None
    assert result.call.duration_ms >= 0


# ── audit trail ─────────────────────────────────────────────────────────────


async def test_response_is_stored_with_parsed_json(client_factory: ClientFactory) -> None:
    client = client_factory([message_payload('{"ok": true}')])
    result = await complete(client)

    assert result.call.response["parsed"] == {"ok": True}
    assert result.call.response["stop_reason"] == "end_turn"


async def test_non_json_response_is_stored_as_text(client_factory: ClientFactory) -> None:
    client = client_factory([message_payload("not json at all")])
    result = await complete(client)

    assert result.call.response["text"] == "not json at all"
    assert "parsed" not in result.call.response


async def test_request_audit_is_stored_verbatim(client_factory: ClientFactory) -> None:
    audit = {"system": "S", "text_blocks": ["a", "b"], "images": [{"sha256": "abc"}]}
    client = client_factory([message_payload('{"ok": true}')])
    result = await complete(client, request_audit=audit)

    assert result.call.request == audit


async def test_failed_call_still_produces_a_record(client_factory: ClientFactory) -> None:
    """The point of recording in the client: a failure must not lose its row."""
    client = client_factory([error_response(500)])
    result = await complete(client)

    assert result.call.model == "claude-fable-5"
    assert result.call.prompt_version == "fable_v1"
    assert result.call.kind is LLMCallKind.ANALYST
    assert result.call.started_at is not None


# ── cost logging (CLAUDE.md §Code standards) ────────────────────────────────


async def test_log_line_carries_every_mandated_field(client_factory: ClientFactory) -> None:
    import structlog

    with structlog.testing.capture_logs() as logs:
        client = client_factory([message_payload('{"ok": true}')])
        await complete(client)

    entry = next(line for line in logs if line["event"] == "llm.call")
    for field in (
        "model",
        "prompt_version",
        "tokens_in",
        "tokens_out",
        "cost_usd_estimate",
        "duration_ms",
    ):
        assert field in entry, f"CLAUDE.md requires {field} on every LLM call log line"


async def test_log_line_never_carries_the_api_key(client_factory: ClientFactory) -> None:
    import structlog

    with structlog.testing.capture_logs() as logs:
        client = client_factory([message_payload('{"ok": true}')])
        await complete(client)

    assert "test-key-not-a-real-secret" not in json.dumps(logs)
