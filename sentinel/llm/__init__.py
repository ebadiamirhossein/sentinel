"""Shared LLM access: one Anthropic client wrapper, used by both tiers.

Not in ARCHITECTURE.md §3's original tree, and deliberately so (owner-approved,
2026-08-18). Both ``screener/`` and ``analyst/`` need the same retry, timeout and
cost-logging policy; having one import the other would be exactly the sideways
import between pipeline stages that CLAUDE.md forbids. So this package sits
*below* both, next to ``storage/``, and depends on nothing but ``core/``.

It is also where M10's second provider will get its client from, unchanged
(specs/ENSEMBLE.md §5: "route through the shared retry/backoff client wrapper").
"""
