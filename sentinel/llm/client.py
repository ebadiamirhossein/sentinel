"""The one place this system talks to Anthropic.

Policy lives here so both tiers get it identically (CLAUDE.md §Code standards):
per-call timeout, bounded transport retries, and one structured log line per call
carrying model, prompt version, tokens in/out, cost estimate and duration.

:meth:`AnthropicClient.complete` **does not raise for expected API outcomes.** A
refusal, a truncation, a timeout and a 500 all come back as an
:class:`~sentinel.llm.models.LLMCall` with a status, alongside the message when
there is one. That is on purpose: PRD F10 requires the failed calls in the audit
trail, and an exception thrown before the record exists is a lost record. The
caller decides what a status means for the pipeline; this class only reports.

**Model-specific request rules for ``claude-fable-5``** (each one is a 400 if
broken, and each has a test in ``tests/llm/test_request_shape.py``):

* ``thinking`` must be **omitted entirely** -- thinking is always on, and both
  ``{"type": "disabled"}`` and ``{"type": "enabled", "budget_tokens": N}`` are
  rejected. Depth is controlled by ``output_config.effort`` instead.
* ``temperature`` / ``top_p`` / ``top_k`` are removed from the API; sending any
  of them is rejected. This class never sets them, so a future caller cannot.
* No assistant prefill: the last message is always ``role="user"``.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

import anthropic
import httpx
from anthropic import omit
from anthropic.types import Message, MessageParam, TextBlock, TextBlockParam
from anthropic.types.output_config_param import OutputConfigParam

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import LLMConfig
from sentinel.core.logging import get_logger
from sentinel.llm.models import LLMCall, LLMCallKind, LLMCallStatus, TokenUsage
from sentinel.llm.pricing import estimate_cost

log = get_logger(__name__)

PROVIDER = "anthropic"


class LLMResult:
    """A completed attempt: the message if there is one, and the audit record.

    Not a Pydantic model -- ``Message`` is the SDK's own type and wrapping it in a
    validated container buys nothing. ``call`` is always present; ``message`` is
    ``None`` whenever the request never produced one.
    """

    __slots__ = ("call", "message")

    def __init__(self, message: Message | None, call: LLMCall) -> None:
        self.message = message
        self.call = call

    @property
    def ok(self) -> bool:
        return self.call.status is LLMCallStatus.OK

    def text(self) -> str:
        """Concatenated text blocks. Thinking blocks carry no text and are skipped.

        With ``output_config.format`` set, this is the JSON document.
        """
        return text_of(self.message)


class AnthropicClient:
    """Shared Messages API client. Construct once per process."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        api_key: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or SystemClock()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            max_retries=config.max_transport_retries,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(
        self,
        *,
        kind: LLMCallKind,
        model: str,
        prompt_version: str,
        system: str,
        messages: list[MessageParam],
        max_tokens: int,
        timeout_seconds: float,
        effort: str | None = None,
        output_schema: dict[str, Any] | None = None,
        request_audit: dict[str, Any],
        symbol: str | None = None,
        cycle_id: UUID | None = None,
        attempt: int = 1,
    ) -> LLMResult:
        """Make one call. Never raises for an API outcome -- inspect ``call.status``.

        ``request_audit`` is what gets stored: the caller builds it because only
        the caller knows how to reference its own inputs (the analyst records
        chart images as ``{sha256, params}``, not bytes).
        """
        output_config: OutputConfigParam = {}
        if effort is not None:
            output_config["effort"] = effort  # type: ignore[typeddict-item]
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}

        # System is a cacheable prefix: stable prompt text first, volatile
        # per-symbol content in `messages` (shared/prompt-caching -- the prefix
        # must not move). One breakpoint, on the only system block.
        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

        started_at = self._clock.now()
        started = time.monotonic()
        message: Message | None = None
        status = LLMCallStatus.OK
        error: str | None = None
        request_id: str | None = None

        try:
            # NOTE: no `thinking`, no `temperature`/`top_p`/`top_k` -- see module
            # docstring. Do not add them for claude-fable-5.
            message = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=messages,
                timeout=timeout_seconds,
                output_config=output_config if output_config else omit,
            )
            request_id = message._request_id
        except anthropic.APITimeoutError as exc:
            status, error = LLMCallStatus.TIMEOUT, f"{type(exc).__name__}: {exc}"
        except anthropic.APIStatusError as exc:
            status = LLMCallStatus.API_ERROR
            error = f"HTTP {exc.status_code} {exc.type or ''}: {exc.message}".strip()
            request_id = exc.request_id
        except (anthropic.APIConnectionError, httpx.HTTPError) as exc:
            status, error = LLMCallStatus.API_ERROR, f"{type(exc).__name__}: {exc}"

        duration_ms = int((time.monotonic() - started) * 1000)

        usage = _usage_of(message)
        stop_reason = message.stop_reason if message else None
        refusal_category: str | None = None

        if message is not None:
            if stop_reason == "refusal":
                status = LLMCallStatus.REFUSAL
                details = message.stop_details
                refusal_category = getattr(details, "category", None)
                error = getattr(details, "explanation", None) or "declined by safety classifiers"
            elif stop_reason == "max_tokens":
                # Truncated JSON is unusable. Surfacing it as its own status keeps
                # "the model got it wrong" separate from "we did not give it room",
                # which are different fixes.
                status = LLMCallStatus.TRUNCATED
                error = f"output truncated at max_tokens={max_tokens}"

        call = LLMCall(
            cycle_id=cycle_id,
            symbol=symbol,
            kind=kind,
            provider=PROVIDER,
            model=model,
            prompt_version=prompt_version,
            attempt=attempt,
            status=status,
            stop_reason=stop_reason,
            refusal_category=refusal_category,
            error=error,
            usage=usage,
            cost_usd_estimate=estimate_cost(model, usage, self._config.pricing),
            duration_ms=duration_ms,
            request_id=request_id,
            started_at=started_at,
            request=request_audit,
            response=_response_audit(message),
        )

        # The line CLAUDE.md mandates: model, prompt_version, tokens in/out, cost,
        # duration -- on every call, successful or not.
        log.info(
            "llm.call",
            kind=kind.value,
            provider=PROVIDER,
            model=model,
            prompt_version=prompt_version,
            symbol=symbol,
            attempt=attempt,
            status=status.value,
            stop_reason=stop_reason,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd_estimate=str(call.cost_usd_estimate),
            duration_ms=duration_ms,
            request_id=request_id,
            error=error,
        )
        return LLMResult(message, call)


def text_of(message: Message | None) -> str:
    """Concatenated text blocks. Thinking blocks carry no text and are skipped."""
    if message is None:
        return ""
    return "".join(block.text for block in message.content if isinstance(block, TextBlock))


def _usage_of(message: Message | None) -> TokenUsage:
    if message is None:
        return TokenUsage()
    usage = message.usage
    return TokenUsage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )


def _response_audit(message: Message | None) -> dict[str, Any]:
    """Store the response as received. Never truncated -- this is the audit trail."""
    if message is None:
        return {}
    dumped = message.model_dump(mode="json")
    text = text_of(message)
    if text:
        # Keep the parsed form alongside the raw blocks when it is JSON, so a
        # query can reach into the verdict without re-parsing in SQL.
        try:
            dumped["parsed"] = json.loads(text)
        except ValueError:
            dumped["text"] = text
    return dumped
