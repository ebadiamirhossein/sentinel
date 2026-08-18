"""Wire-level Anthropic doubles, shared by tests/llm, tests/screener and tests/analyst.

The SDK is driven through an ``httpx.MockTransport``, not a mocked-out client
object. That is the same choice M1 made for the REST clients, for the same
reason: it exercises the request *we actually build* -- model, effort, schema,
image blocks, absence of ``thinking`` -- rather than a stand-in for it. A mock
that only records ``complete(...)`` arguments would happily pass while sending a
400-inducing body.

Every handler records the decoded request bodies, so a test can assert on what
went over the wire.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx

from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.llm.client import AnthropicClient

#: The `client_factory` fixture's type. Aliased so every test package spells it
#: the same way under mypy --strict.
ClientFactory = Callable[..., AnthropicClient]

#: Anchor for every recorded exchange in this package.
LLM_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def message_payload(
    text: str,
    *,
    model: str = "claude-fable-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 1200,
    output_tokens: int = 300,
    cache_read: int = 0,
    cache_write: int = 0,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Messages API response body, shaped as the API returns one."""
    body: dict[str, Any] = {
        "id": "msg_01TestMessageIdentifier",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return body


class Recorder:
    """Captures every request body the SDK sent, in order."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def __len__(self) -> int:
        return len(self.requests)


def scripted_transport(
    responses: list[dict[str, Any] | httpx.Response],
    recorder: Recorder,
) -> httpx.MockTransport:
    """Replay ``responses`` in order; the last one repeats if we run past the end."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(json.loads(request.content.decode()))
        index = min(len(recorder.requests) - 1, len(responses) - 1)
        response = responses[index]
        if isinstance(response, httpx.Response):
            # httpx.Response objects are single-use once read; rebuild each time.
            return httpx.Response(
                response.status_code,
                json=json.loads(response.content.decode()) if response.content else None,
                headers={"request-id": "req_test"},
            )
        return httpx.Response(200, json=response, headers={"request-id": "req_test"})

    return httpx.MockTransport(handler)


def error_response(status_code: int, error_type: str = "api_error") -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"type": "error", "error": {"type": error_type, "message": f"test {status_code}"}},
    )


def make_client(
    transport: httpx.MockTransport,
    config: AppConfig,
    *,
    max_retries: int | None = None,
) -> AnthropicClient:
    sdk = anthropic.AsyncAnthropic(
        api_key="test-key-not-a-real-secret",
        max_retries=config.llm.max_transport_retries if max_retries is None else max_retries,
        http_client=httpx.AsyncClient(transport=transport),
    )
    return AnthropicClient(config.llm, client=sdk, clock=FrozenClock(LLM_NOW))
