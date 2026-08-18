"""Analyst providers behind one protocol (specs/ENSEMBLE.md §2).

``anthropic_fable.py`` ships in M5. ``openai_sol.py`` arrives at M10 and
implements the same protocol against the same schema, so shadow mode is a second
``analyze()`` call in an ``asyncio.gather`` rather than a refactor.
"""
