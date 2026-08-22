"""The two rails a Persian summary must pass before anybody reads it.

**One module for both, deliberately.** They are the only two fail-closed checks on
this path and they cover each other's blind spot: the numbers rail cannot see a
softened verdict, because softening invents no number, and the verdict rail cannot
see an invented price. Splitting them across two files is how one of them gets
reviewed, loosened or deleted without the other being in front of the reader. The
module name now under-describes what is in it, which is the smaller cost.

---

**THE NUMBERS RAIL:** every number the Persian summary prints must be on the card.

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
from enum import StrEnum

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


# ---------------------------------------------------------------------------
# THE VERDICT RAIL
# ---------------------------------------------------------------------------
#
# The numbers rail is structurally blind to the one failure that would be invisible in
# review and expensive in practice: a WATCHLIST rewritten as an encouraging card.
# Softening a verdict invents no number, so nothing above can see it, and HARD BOUNDARY
# 5 in the prompt is a request rather than a rail.
#
# **Where this actually bites.** A signal card exists only for a gate-approved plan --
# ``_check_preconditions`` returns ``NOT_A_CANDIDATE`` before a plan is built -- so on
# that surface the expected verdict is a constant and this check is weak, catching only
# the safe-direction mirror (an approved setup described discouragingly). The surface
# that matters is ``/pulse SYMBOL``, which renders WATCHLIST and NO_SETUP verdicts --
# **and which carries almost no numbers at all.** Of the fifteen numeric tokens on the
# golden pulse card, six are parts of a date, two are confidence and four are timeframe
# labels; three are levels, and all three come from prose rather than from a plan. So on
# the one surface where a softened verdict is reachable, the numbers rail admits nearly
# anything and this is the only rail there is.
#
# **The verdict is never parsed out of the summary, or out of the card.** It is supplied
# by the caller, from the report the card was rendered from. A check that read the
# model's own text to decide what the model was supposed to say would be agreeing with
# itself.


class VerdictClass(StrEnum):
    """What the card permits the reader to do. Two classes, not three.

    ``WATCHLIST`` and ``NO_SETUP`` share :attr:`NOT_YET` because the distinction this
    rail exists to protect is *actionable vs not*. Separating them would catch a
    NO_SETUP that reads like a WATCHLIST -- a far less consequential confusion -- at the
    cost of a third marker set and a third way to fail a true summary. Every rail here
    is fail-closed, so every widening of its vocabulary is a widening of the ways a good
    summary gets thrown away.
    """

    #: The gate approved a plan. The reader may act on it.
    ACTIONABLE = "ACTIONABLE"
    #: WATCHLIST or NO_SETUP. Watch; do not buy.
    NOT_YET = "NOT_YET"


#: The first character of the verdict line, per class. Disjoint by construction, so a
#: marker from the wrong set is always a failure rather than a silent pass.
#:
#: Markers rather than a phrase vocabulary, and that choice is the difference between a
#: rail and a nuisance. Persian has many ways to say "do not buy" -- نخر, صبر کن, فقط
#: تماشا, وارد نشو, دست نگه دار -- and a fixed *phrase* list would reject the ones it
#: had not thought of, which is a rail firing wrongly on a true summary. A single
#: leading emoji is something the prompt can mandate exactly, the owner's own examples
#: already do it, and the measured first call already produced it unprompted.
VERDICT_MARKERS: dict[VerdictClass, frozenset[str]] = {
    VerdictClass.ACTIONABLE: frozenset({"✅"}),
    VerdictClass.NOT_YET: frozenset({"❌", "⛔", "👀"}),
}


def verdict_class(candidate_status: str) -> VerdictClass:
    """``CANDIDATE`` is actionable; everything else is not.

    Written against the *string* rather than against ``CandidateStatus`` so this module
    keeps importing nothing from the analyst package -- it is a rail, and a rail that
    grows a dependency on the thing it is checking is on its way to agreeing with it.
    An unrecognised status falls to ``NOT_YET``, which is the safe direction: a verdict
    this rail does not understand must not be allowed to read as a buy.
    """
    return (
        VerdictClass.ACTIONABLE if candidate_status.upper() == "CANDIDATE" else VerdictClass.NOT_YET
    )


class VerdictCheck(BaseModel):
    """The verdict verdict, and what the log needs to say."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: VerdictClass
    #: The first non-empty line of the summary, as found. Empty when there is none.
    verdict_line: str = ""
    ok: bool = False

    @property
    def detail(self) -> str:
        if self.ok:
            return f"the verdict line matches {self.expected.value}"
        allowed = " ".join(sorted(VERDICT_MARKERS[self.expected]))
        if not self.verdict_line:
            return f"no verdict line at all; expected one starting with {allowed}"
        return (
            f"verdict line {self.verdict_line!r} does not start with "
            f"{allowed} — the card is {self.expected.value}"
        )


def check_verdict(*, summary: str, candidate_status: str) -> VerdictCheck:
    """Does the summary's opening line agree with the card's verdict?

    The prompt requires the first line to be the verdict and to begin with one of the
    markers for its class, so this pins something that is already there rather than
    imposing a shape on the Persian.

    **What it does not prove.** It checks the *marker*, not the meaning: a summary
    reading ``❌ بخر`` would pass. That residual is deliberate -- catching it needs a
    phrase vocabulary, and a phrase vocabulary is where the false failures live. The
    marker is the part a model gets wrong when it has drifted into the wrong register,
    which is the failure this is for.
    """
    expected = verdict_class(candidate_status)
    lines = [line.strip() for line in plain_text(summary).splitlines()]
    first = next((line for line in lines if line), "")
    return VerdictCheck(
        expected=expected,
        verdict_line=first,
        ok=any(first.startswith(marker) for marker in VERDICT_MARKERS[expected]),
    )


__all__ = [
    "NON_ASCII_DIGITS",
    "VERDICT_MARKERS",
    "NumberCheck",
    "VerdictCheck",
    "VerdictClass",
    "canonical",
    "check_numbers",
    "check_verdict",
    "numeric_tokens",
    "plain_text",
    "verdict_class",
]
