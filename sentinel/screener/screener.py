"""The cheap triage pass: all watchlist symbols in, 0-3 candidates out.

specs/PROMPTS.md §1. One batch call per cycle (or per ``screener_batch_size``
chunk), strict JSON, then a **deterministic reconciliation** step that the LLM
does not get a say in:

* a symbol the model omitted becomes ``interesting=false`` and is logged -- an
  unanswered symbol is not an interesting one;
* a symbol the model invented is dropped and logged -- it corresponds to no
  snapshot, so there is nothing to analyze even if it were interesting;
* the output is always exactly one verdict per submitted symbol, in submission
  order.

The prompt asks for that shape; this code guarantees it. A screener that fails
twice yields **zero** candidates rather than a fabricated one: an empty cycle is
correct behaviour (§1: "nothing is happening" is the expected answer), whereas a
guessed candidate costs a deep-analysis call and possibly a signal.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from anthropic.types import MessageParam
from pydantic import ValidationError

from sentinel.analyst.prompts.loader import load_prompt
from sentinel.core.config import AppConfig
from sentinel.core.logging import get_logger
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import MarketSnapshot
from sentinel.llm.client import AnthropicClient, LLMResult
from sentinel.llm.models import LLMCall, LLMCallKind, LLMCallStatus
from sentinel.llm.schema import json_schema_for
from sentinel.screener.models import DirectionHint, ScreenerBatch, ScreenerResult, ScreenerVerdict
from sentinel.screener.payload import screener_block

log = get_logger(__name__)

PROMPT_VERSION = "screener_v1"


def not_interesting(symbol: str, reason: str) -> ScreenerVerdict:
    """The safe default. Used whenever the model did not answer for a symbol."""
    return ScreenerVerdict(
        symbol=symbol,
        interesting=False,
        direction_hint=DirectionHint.UNCLEAR,
        reason=reason[:200],
    )


def reconcile(
    symbols: list[str], returned: tuple[ScreenerVerdict, ...]
) -> tuple[ScreenerVerdict, ...]:
    """Force the model's answer onto the symbols we actually asked about.

    Pure, and tested independently of any API call -- this is the function that
    stops a hallucinated symbol reaching the deep analyst.
    """
    by_symbol = {verdict.symbol.upper(): verdict for verdict in returned}

    unknown = sorted(set(by_symbol) - {symbol.upper() for symbol in symbols})
    if unknown:
        log.warning("screener.unknown_symbols", symbols=unknown, requested=symbols)

    verdicts: list[ScreenerVerdict] = []
    missing: list[str] = []
    for symbol in symbols:
        verdict = by_symbol.get(symbol.upper())
        if verdict is None:
            missing.append(symbol)
            verdicts.append(not_interesting(symbol, "no verdict returned by the screener"))
        else:
            # Keep our spelling of the symbol, not the model's.
            verdicts.append(verdict.model_copy(update={"symbol": symbol}))

    if missing:
        log.warning("screener.missing_symbols", symbols=missing, requested=symbols)
    return tuple(verdicts)


class Screener:
    """Cheap-tier batch triage."""

    def __init__(
        self,
        client: AnthropicClient,
        config: AppConfig,
        *,
        prompt_version: str | None = None,
        cycle_id: UUID | None = None,
    ) -> None:
        self._client = client
        self._config = config
        # Config wins; PROMPT_VERSION is the floor for a config that predates the
        # setting, and the explicit argument is for tests that pin a version.
        self.prompt_version = prompt_version or config.llm.screener_prompt_version
        self._cycle_id = cycle_id

    async def screen(
        self,
        snapshots: list[MarketSnapshot],
        features: dict[str, SymbolFeatures],
    ) -> ScreenerResult:
        """Triage every snapshot. Never raises -- a failure yields no candidates."""
        usable = [snapshot for snapshot in snapshots if snapshot.symbol in features]
        if not usable:
            return ScreenerResult()

        verdicts: list[ScreenerVerdict] = []
        calls: list[LLMCall] = []
        degraded = False

        for chunk in _chunk(usable, self._config.llm.screener_batch_size):
            chunk_verdicts, chunk_calls, chunk_degraded = await self._screen_chunk(chunk, features)
            verdicts.extend(chunk_verdicts)
            calls.extend(chunk_calls)
            degraded = degraded or chunk_degraded

        result = ScreenerResult(verdicts=tuple(verdicts), calls=tuple(calls), degraded=degraded)
        log.info(
            "screener.batch_complete",
            symbols=len(verdicts),
            interesting=len(result.interesting),
            calls=len(calls),
            degraded=degraded,
            cost_usd_estimate=str(sum((call.cost_usd_estimate for call in calls), start=0)),
        )
        return result

    async def _screen_chunk(
        self,
        snapshots: list[MarketSnapshot],
        features: dict[str, SymbolFeatures],
    ) -> tuple[tuple[ScreenerVerdict, ...], list[LLMCall], bool]:
        symbols = [snapshot.symbol for snapshot in snapshots]
        blocks = [screener_block(snapshot, features[snapshot.symbol]) for snapshot in snapshots]
        body = json.dumps(blocks, indent=2, sort_keys=True)
        user_text = (
            f"Triage these {len(symbols)} symbols. Return exactly one verdict per "
            f"symbol listed, and no others.\n\n{body}"
        )

        system = load_prompt(self.prompt_version)
        schema = json_schema_for(ScreenerBatch)
        messages: list[MessageParam] = [{"role": "user", "content": user_text}]
        calls: list[LLMCall] = []
        attempts = self._config.llm.max_json_retries + 1

        for attempt in range(1, attempts + 1):
            result = await self._client.complete(
                kind=LLMCallKind.SCREENER,
                model=self._config.llm.screener_model,
                prompt_version=self.prompt_version,
                system=system,
                messages=messages,
                max_tokens=self._config.llm.screener_max_tokens,
                timeout_seconds=self._config.llm.screener_timeout_seconds,
                output_schema=schema,
                request_audit={"system": system, "text_blocks": [user_text], "images": []},
                cycle_id=self._cycle_id,
                attempt=attempt,
            )
            calls.append(result.call)

            if result.call.status in {
                LLMCallStatus.REFUSAL,
                LLMCallStatus.TIMEOUT,
                LLMCallStatus.API_ERROR,
            }:
                log.warning(
                    "screener.unavailable",
                    status=result.call.status.value,
                    error=result.call.error,
                    symbols=symbols,
                )
                break

            batch, problem = _parse(result)
            if batch is not None:
                return reconcile(symbols, batch.verdicts), calls, False

            calls[-1] = result.call.model_copy(
                update={"status": LLMCallStatus.INVALID_JSON, "error": problem}
            )
            log.warning(
                "screener.invalid_output", attempt=attempt, max_attempts=attempts, problem=problem
            )
            if attempt < attempts:
                messages = [
                    *messages,
                    {"role": "assistant", "content": result.text() or "(empty response)"},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not satisfy the schema and was "
                            f"rejected:\n\n{problem}\n\nReturn the corrected JSON only."
                        ),
                    },
                ]

        # Discarded: every symbol defaults to not-interesting, and the cycle goes
        # on with zero candidates.
        log.warning("screener.batch_discarded", symbols=symbols, attempts=len(calls))
        return (
            tuple(not_interesting(symbol, "screener unavailable this cycle") for symbol in symbols),
            calls,
            True,
        )


def _parse(result: LLMResult) -> tuple[ScreenerBatch | None, str]:
    text = result.text().strip()
    if not text:
        return None, "empty response body"
    try:
        return ScreenerBatch.model_validate_json(text), ""
    except ValidationError as exc:
        return None, f"{exc.error_count()} schema error(s): {exc}"


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    """``size <= 0`` means one batch for everything (the configured default)."""
    if size <= 0 or size >= len(items):
        return [items]
    return [items[start : start + size] for start in range(0, len(items), size)]
