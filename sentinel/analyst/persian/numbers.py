"""The numbers rail: every number the Persian summary prints must be on the card.

**Why a check and not only a prompt rule.** If the Persian card and the English card
ever disagree about a stop price, the owner has two systems telling him different
things about real money. A prompt instruction is a request the model can decline; this
is the part that cannot be declined. It **fails closed** -- a summary that does not
pass is not sent and not stored, and the user is told so in Persian.

**What it proves.** Every numeric token in the output also appears in the input, as a
token, after one normalisation applied identically to both sides. That is strictly
stronger than checking only prices, and it needs no understanding of the card: working
out *which* token is a price would require the checker to read the card, which is the
one thing this whole design refuses to let anything downstream of the analyst do.

**What it cannot prove**, stated here so nobody mistakes its scope: a number copied
correctly but attached to the wrong label -- a stop presented as a target -- passes.
Containment is a claim about provenance, not about meaning. A label-adjacency check is
possible and brittle; it is an open item, not a silent gap.

**ASCII digits only, deliberately.** The rule is that numbers are copied
character-for-character, and a Persian-digit form is by definition not that. Allowing
U+06F0..U+06F9 would mean normalising two numeral systems on the one rail that must
never be wrong, to buy a rendering preference. Every price on every card is ASCII already. So a
Persian or Arabic-Indic digit anywhere in the output is an outright failure rather than
something to convert and compare -- the check agrees with the prompt instead of
quietly widening it.

Pure: no clock, no database, no LLM, no I/O.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

#: Extended Arabic-Indic (Persian) and Arabic-Indic digits. Their presence in a
#: summary is a failure, never an input to normalisation -- see the module docstring.
#: Written as escapes rather than as the characters themselves: the two ranges are
#: visually confusable with ASCII in most fonts, which is the whole reason they are
#: rejected rather than converted.
NON_ASCII_DIGITS = re.compile("[\u06f0-\u06f9\u0660-\u0669]")

#: One regex, applied to BOTH sides. Tokenising input and output the same way is what
#: makes ``EMA200`` on the card and ``EMA 200`` in the summary both yield ``200``: the
#: comparison is over tokens, not over the strings that contain them.
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")

#: Telegram HTML markup. Stripped from both sides so ``<b>64150.0</b>`` and ``64150.0``
#: tokenise identically.
_TAG = re.compile(r"<[^>]*>")

_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


def plain_text(text: str) -> str:
    """Markup removed and the three escaped entities restored.

    ``&amp;`` is undone last: doing it first would turn a literal ``&amp;lt;`` -- which
    is how a card renders the text ``&lt;`` -- into a tag boundary that was never there.
    """
    stripped = _TAG.sub(" ", text)
    for entity, char in _ENTITIES:
        stripped = stripped.replace(entity, char)
    return stripped


def canonical(token: str) -> str:
    """A numeric token with thousands separators removed, and nothing else changed.

    The line this draws is deliberate. A thousands separator is a **grouping mark**:
    removing it cannot change what the number is, so ``64,150.0`` and ``64150.0`` are
    the same number written for two audiences. A dropped trailing zero is not --
    ``4570.3`` and ``4570.30`` differ in stated precision, which on a stop price is a
    claim about how exact the level is. So the first is normalised away and the second
    fails, which is what "never reformatted" in the brief has to mean if it is to mean
    anything checkable.
    """
    return token.replace(",", "")


def numeric_tokens(text: str) -> list[str]:
    """Every numeric token in ``text``, canonicalised, in the order they appear."""
    return [canonical(match.group()) for match in _NUMBER.finditer(plain_text(text))]


class NumberCheck(BaseModel):
    """The verdict, and enough detail to log what went wrong without guessing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Tokens in the summary that are absent from the card. Non-empty means failure.
    foreign_tokens: tuple[str, ...] = ()
    #: Non-ASCII digit characters found in the summary. Non-empty means failure.
    non_ascii_digits: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.foreign_tokens and not self.non_ascii_digits

    @property
    def detail(self) -> str:
        """One line for the log. Says which rule broke, not merely that one did."""
        parts = []
        if self.non_ascii_digits:
            parts.append(f"non-ASCII digits: {''.join(sorted(set(self.non_ascii_digits)))}")
        if self.foreign_tokens:
            parts.append(f"numbers not on the card: {', '.join(self.foreign_tokens)}")
        return "; ".join(parts) if parts else "every number in the summary is on the card"


def check_numbers(*, card: str, summary: str) -> NumberCheck:
    """Does ``summary`` invent, recompute, round or reformat any number in ``card``?

    Containment in one direction only: the summary is a short rewrite, so the card is
    expected to hold numbers the summary drops. The reverse is the failure.
    """
    allowed = set(numeric_tokens(card))
    foreign = tuple(dict.fromkeys(t for t in numeric_tokens(summary) if t not in allowed))
    return NumberCheck(
        foreign_tokens=foreign,
        non_ascii_digits=tuple(dict.fromkeys(NON_ASCII_DIGITS.findall(summary))),
    )


__all__ = [
    "NON_ASCII_DIGITS",
    "NumberCheck",
    "canonical",
    "check_numbers",
    "numeric_tokens",
    "plain_text",
]
