"""Typed LLM failures.

Deliberately small. Expected API outcomes -- refusal, malformed output, an
outage -- are *not* exceptions here: :meth:`AnthropicClient.complete` returns
them as a recorded :class:`~sentinel.llm.models.LLMCall` with a status, because
PRD F10 wants the failed calls in the audit trail just as much as the successful
ones, and an exception that escapes before the record is written loses them.

What remains is the one thing the type system has to express: a provider that
produced no usable report.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for every LLM failure."""


class AnalystUnavailable(LLMError):
    """No usable report from a provider this cycle.

    ``AnalystProvider.analyze`` is specified (specs/ENSEMBLE.md §2) to return an
    ``AnalystReport``, so "no report" has to be an exception rather than a
    sentinel. That shape pays off at M10: ENSEMBLE §1 rule 4 ("fail open to
    primary") becomes a ``try/except`` around the shadow provider that logs
    ``second_opinion: UNAVAILABLE`` and carries on.

    ``reason`` is a short machine-ish string (``refusal``, ``invalid_json``,
    ``timeout``) so M9 can group causes; ``detail`` is the prose.
    """

    def __init__(self, provider: str, symbol: str, reason: str, detail: str = "") -> None:
        super().__init__(f"{provider} produced no report for {symbol}: {reason} {detail}".strip())
        self.provider = provider
        self.symbol = symbol
        self.reason = reason
        self.detail = detail
