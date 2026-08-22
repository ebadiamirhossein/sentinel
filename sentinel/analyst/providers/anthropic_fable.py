"""The deep analyst: claude-fable-5, chart vision, schema-validated JSON.

Implements :class:`~sentinel.analyst.providers.protocol.AnalystProvider`. One
symbol in, one ``AnalystReport`` out, or ``AnalystUnavailable`` -- never a guess
(ARCHITECTURE.md §6).

**Retry rule** (specs/PROMPTS.md §2, ``llm.max_json_retries``): invalid output
gets exactly one retry whose user turn carries the validation errors verbatim,
then the report is discarded and logged. That is separate from the transport
retries the SDK does for 429/5xx -- different failure, different budget.

**Chart order is 4h, 1h, 15m** -- context, then setup, then timing, matching the
MULTI-TIMEFRAME PROTOCOL the system prompt walks through. ``config.charts``
lists timeframes in ascending order for rendering; the ordering the model sees is
a prompt concern and lives here.
"""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from anthropic.types import ImageBlockParam, MessageParam, TextBlockParam
from pydantic import ValidationError

from sentinel.analyst.models import AnalystReport, AnalystReportPayload
from sentinel.analyst.prompts.loader import load_prompt
from sentinel.analyst.serialization import analyst_payload, news_block, render_payload
from sentinel.charts.models import ChartImage
from sentinel.core.config import AppConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.models import MarketSnapshot
from sentinel.llm.client import AnthropicClient, LLMResult
from sentinel.llm.errors import AnalystUnavailable
from sentinel.llm.models import LLMCall, LLMCallKind, LLMCallStatus
from sentinel.llm.schema import json_schema_for

log = get_logger(__name__)

#: Context first, setup second, trigger last -- the order the prompt reasons in.
CHART_ORDER = ("4h", "1h", "15m")

PROVIDER_NAME = "fable5"


class AnthropicFableAnalyst:
    """Deep analyst backed by the Anthropic Messages API."""

    name = PROVIDER_NAME

    def __init__(
        self,
        client: AnthropicClient,
        config: AppConfig,
        *,
        prompt_version: str = "fable_v1",
        cycle_id: UUID | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self.prompt_version = prompt_version
        self._cycle_id = cycle_id
        #: Every call this provider made, in order -- the caller persists them.
        #: Populated even for discarded attempts (PRD F10).
        self.calls: list[LLMCall] = []

    async def analyze(
        self,
        snapshot: MarketSnapshot,
        charts: list[ChartImage],
        history_block: str,
    ) -> AnalystReport:
        """One symbol. Raises ``AnalystUnavailable`` rather than inventing a report."""
        system = load_prompt(self.prompt_version)
        schema = json_schema_for(AnalystReportPayload)
        ordered = order_charts(charts, snapshot.symbol)
        user_blocks = self.user_blocks(snapshot, ordered, history_block)
        messages: list[MessageParam] = [{"role": "user", "content": user_blocks}]

        attempts = self._config.llm.max_json_retries + 1
        last_problem = "no attempt made"

        for attempt in range(1, attempts + 1):
            result = await self._client.complete(
                kind=LLMCallKind.ANALYST,
                model=self._config.llm.analyst_model,
                prompt_version=self.prompt_version,
                system=system,
                messages=messages,
                max_tokens=self._config.llm.analyst_max_tokens,
                timeout_seconds=self._config.llm.analyst_timeout_seconds,
                effort=self._config.llm.analyst_effort,
                output_schema=schema,
                request_audit=_request_audit(system, user_blocks, ordered),
                symbol=snapshot.symbol,
                cycle_id=self._cycle_id,
                attempt=attempt,
            )
            self.calls.append(result.call)

            if result.call.status is LLMCallStatus.REFUSAL:
                # No server-side fallback by owner ruling (2026-08-18): a signal
                # must be attributable to the model whose prompt produced it.
                raise AnalystUnavailable(
                    self.name,
                    snapshot.symbol,
                    "refusal",
                    result.call.refusal_category or "",
                )
            if result.call.status in {LLMCallStatus.TIMEOUT, LLMCallStatus.API_ERROR}:
                raise AnalystUnavailable(
                    self.name,
                    snapshot.symbol,
                    result.call.status.value.lower(),
                    result.call.error or "",
                )

            problem = _problem_with(result, snapshot.symbol)
            if problem is None:
                payload = AnalystReportPayload.model_validate_json(result.text())
                return payload.to_report(
                    prompt_version=self.prompt_version,
                    model=result.call.model,
                )

            last_problem = problem
            self.calls[-1] = result.call.model_copy(
                update={"status": LLMCallStatus.INVALID_JSON, "error": problem}
            )
            log.warning(
                "analyst.invalid_output",
                symbol=snapshot.symbol,
                attempt=attempt,
                max_attempts=attempts,
                problem=problem,
            )
            if attempt < attempts:
                # Feed the error back verbatim. The model cannot fix what it is
                # not shown, and paraphrasing a validation error loses the path.
                messages = [
                    *messages,
                    {"role": "assistant", "content": result.text() or "(empty response)"},
                    {"role": "user", "content": _retry_instruction(problem)},
                ]

        raise AnalystUnavailable(self.name, snapshot.symbol, "invalid_json", last_problem)

    # ----------------------------------------------------------------- prompt

    def user_blocks(
        self,
        snapshot: MarketSnapshot,
        charts: list[ChartImage],
        history_block: str,
    ) -> list[TextBlockParam | ImageBlockParam]:
        """The user turn, in order: charts, snapshot, news fence, history, task.

        Public because ``tools/analyze.py --show-prompt`` prints it: the exact
        text the model receives should be readable without a debugger.
        """
        payload = analyst_payload(snapshot, self._config.risk)
        blocks: list[TextBlockParam | ImageBlockParam] = []

        for chart in charts:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(chart.png).decode("ascii"),
                    },
                }
            )
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"Chart above: {chart.params.spec.symbol} {chart.params.spec.timeframe}, "
                        f"{chart.params.candles_drawn} closed candles to "
                        f"{chart.params.last_candle_at.isoformat()}."
                    ),
                }
            )

        blocks.append({"type": "text", "text": f"SNAPSHOT\n{render_payload(payload)}"})
        _append_text(blocks, news_block(snapshot.news, symbol=snapshot.symbol))
        _append_text(blocks, history_block)
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Analyze {snapshot.symbol} now. Output only the JSON document "
                    f"described by the schema."
                ),
            }
        )
        return blocks


def _retry_instruction(problem: str) -> str:
    return (
        "Your previous response did not satisfy the required schema and was "
        f"rejected. The validator reported:\n\n{problem}\n\n"
        "Return the corrected JSON document only. Do not change your analysis to "
        "make it validate -- if the honest verdict is NO_SETUP, say NO_SETUP with "
        "null prices."
    )


def _append_text(blocks: list[TextBlockParam | ImageBlockParam], text: str) -> None:
    """Add a text block, or nothing at all if there is no text (M10d).

    **An empty text block is an HTTP 400**, not an empty section: the Messages API
    answers ``messages: text content blocks must be non-empty`` and the whole call
    fails. The forex path passed ``""`` as its history block, so *every* forex analyst
    call would have died that way — as an ``AnalystUnavailable`` that reads in the logs
    like an API problem rather than like a bug (journal/M10d_REPORT.md, join 4).

    Fixed in both places, deliberately. The caller now sends a real history block, and
    this makes the failure impossible rather than merely absent — the next caller with
    an empty section should get no section, not a 400.

    Crypto's bytes cannot move: ``build_history_block`` and ``news_block`` both always
    emit their headers, so neither has ever been empty, and the prompt golden proves it.
    """
    if text.strip():
        blocks.append({"type": "text", "text": text})


def order_charts(charts: list[ChartImage], symbol: str) -> list[ChartImage]:
    """4h, 1h, 15m. Any timeframe not in that list is dropped, loudly."""
    by_timeframe = {chart.params.spec.timeframe: chart for chart in charts}
    ordered = [by_timeframe[tf] for tf in CHART_ORDER if tf in by_timeframe]
    missing = [tf for tf in CHART_ORDER if tf not in by_timeframe]
    if missing:
        # Not fatal: PROMPTS §2 boundary 2 tells the analyst to be stricter when
        # inputs are missing, and it can see which charts it did and did not get.
        log.warning("analyst.charts_missing", symbol=symbol, missing=missing)
    return ordered


def _request_audit(
    system: str,
    blocks: list[TextBlockParam | ImageBlockParam],
    charts: list[ChartImage],
) -> dict[str, Any]:
    """The stored request: text verbatim, images by reference.

    Owner ruling (2026-08-18): ~400KB of PNG per analyst call does not go in
    Postgres. M3 proved a chart re-renders byte-identically from stored OHLCV, so
    sha256 + ``ChartRenderParams`` reconstructs the exact image PRD F4 asks for.
    """
    text_blocks = [block["text"] for block in blocks if block["type"] == "text"]
    return {
        "system": system,
        "text_blocks": text_blocks,
        "images": [
            {"sha256": chart.sha256, "params": chart.params.to_json_dict()} for chart in charts
        ],
    }


def _problem_with(result: LLMResult, symbol: str) -> str | None:
    """Validate the response. Returns a description of the problem, or None."""
    if result.call.status is LLMCallStatus.TRUNCATED:
        return result.call.error or "response truncated"

    text = result.text().strip()
    if not text:
        return "empty response body"

    try:
        payload = AnalystReportPayload.model_validate_json(text)
    except ValidationError as exc:
        return f"{exc.error_count()} schema error(s): {exc}"

    if payload.symbol.upper() != symbol.upper():
        # A mislabelled report must never be routed onto another instrument.
        # Rejecting is right: silently rewriting the symbol would hide the fact
        # that the model lost track of what it was looking at.
        return f"report is for {payload.symbol!r} but {symbol!r} was requested"
    return None
