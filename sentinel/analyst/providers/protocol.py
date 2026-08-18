"""The provider seam, defined in M5 so M10 needs no rework (specs/ENSEMBLE.md §2).

The signature is the spec's, unchanged::

    class AnalystProvider(Protocol):
        name: str  # "fable5" | "gpt56sol"
        async def analyze(self, snapshot, charts, history_block) -> AnalystReport: ...

Two consequences of keeping it literal, both deliberate:

* **Config limits travel through the constructor, not the signature.** The
  analyst is told the gate's minimum RR and maximum entry distance
  (specs/PROMPTS.md §2 "Inputs"), but a provider is built per config and lives
  for the process, so that belongs in ``__init__``. It keeps the call site in
  ENSEMBLE §3 -- two providers, identical arguments -- exactly as written.
* **"No report" is an exception, not a sentinel.** The return type is
  ``AnalystReport``, so a discarded answer raises
  :class:`~sentinel.llm.errors.AnalystUnavailable`. ENSEMBLE §1 rule 4 ("fail
  open to primary") then becomes an ordinary ``try/except`` around the shadow
  call.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sentinel.analyst.models import AnalystReport
from sentinel.charts.models import ChartImage
from sentinel.ingestion.models import MarketSnapshot


@runtime_checkable
class AnalystProvider(Protocol):
    """One deep-analysis provider. Same inputs, same schema, same retry rule."""

    #: Stable identifier stored with every report: "fable5", later "gpt56sol".
    name: str

    #: Which prompt file produced this provider's output, e.g. "fable_v1".
    prompt_version: str

    async def analyze(
        self,
        snapshot: MarketSnapshot,
        charts: list[ChartImage],
        history_block: str,
    ) -> AnalystReport:
        """Analyze one symbol, or raise ``AnalystUnavailable``. Never guesses."""
        ...
