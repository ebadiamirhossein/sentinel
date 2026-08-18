"""Load prompt text from the versioned files next to this module.

CLAUDE.md is categorical: prompt text lives ONLY in these files, and a behaviour
change is a new version file, never an edit in place. Loading rather than
inlining is what makes that enforceable -- ``tests/analyst/test_prompts.py``
asserts the string the client sends is byte-identical to the file on disk, so an
inline "quick tweak" fails the suite instead of silently shipping.

Files carry an HTML comment header (provenance + changelog pointer) above a
``--- SYSTEM PROMPT BELOW ---`` marker. Only what follows the marker is sent, so
the bookkeeping costs no tokens and cannot be mistaken for an instruction.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

MARKER = "--- SYSTEM PROMPT BELOW ---"

PACKAGE = "sentinel.analyst.prompts"


@cache
def load_prompt(version: str) -> str:
    """Return the system prompt text for ``version`` (e.g. ``"fable_v1"``).

    Cached: the text is immutable by policy, and the analyst reads it on every
    call. Raises ``FileNotFoundError`` with the available versions listed, since
    the usual cause is a typo in a config value.
    """
    try:
        raw = resources.files(PACKAGE).joinpath(f"{version}.md").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"no prompt file for version {version!r}; available: {', '.join(available())}"
        ) from exc

    _, separator, body = raw.partition(MARKER)
    if not separator:
        raise ValueError(f"{version}.md is missing the {MARKER!r} marker")
    return body.strip()


def available() -> list[str]:
    """Every prompt version present, for error messages and the M9 audit."""
    return sorted(
        entry.name.removesuffix(".md")
        for entry in resources.files(PACKAGE).iterdir()
        if entry.name.endswith(".md")
    )
