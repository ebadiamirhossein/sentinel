"""Is this text something Telegram's HTML parser will actually accept?

Not a test module — a helper the card tests import. It exists because of a defect
that reached production on 2026-08-20 (journal/M8_6_REPORT.md §11): ``/snapshot``
rendered a perfectly good card locally, and every send failed with

    Bad Request: can't parse entities: Unsupported start tag "200" at byte offset 414

The card carried the feature engine's EMA-stack label — ``20>50<200`` — unescaped.
Telegram read ``<200`` as an opening tag, refused the whole message, and aiogram
logged the exception where nobody was looking. The caller got silence.

**Python's own ``html.parser`` would not have caught it.** It treats ``<200`` as
literal text, because HTML tag names cannot begin with a digit. Telegram's parser is
stricter: *every* ``<`` must open a tag it supports. So this validator encodes
Telegram's rule rather than reaching for the standard library's, which is the whole
reason it is written by hand.

Used as a sweep over rendered cards, it catches the entire class — any field that
can contain a ``<`` and was interpolated without ``escape()`` — rather than the one
instance that happened to reach production.
"""

from __future__ import annotations

import re

#: Every tag Telegram's HTML parse mode accepts (Bot API "Formatting options").
#: Anything else is an "Unsupported start tag", which fails the *whole* message.
SUPPORTED = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "a",
        "code",
        "pre",
        "blockquote",
        "span",
        "tg-spoiler",
        "tg-emoji",
    }
)

#: A ``<`` that opens something tag-shaped. Deliberately requires a letter first,
#: exactly as Telegram does — ``<200`` matches nothing here and is reported.
_TAG = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)")


def unsupported(text: str) -> list[str]:
    """Every ``<`` in ``text`` that does not open a tag Telegram supports.

    Returns a short excerpt at each offence, because "there is a bad ``<``
    somewhere in 2,000 characters" is not a failure message anybody can act on.
    """
    offences: list[str] = []
    for index, character in enumerate(text):
        if character != "<":
            continue
        match = _TAG.match(text, index)
        if match is None or match.group(2).lower() not in SUPPORTED:
            offences.append(text[index : index + 24])
    return offences


def unbalanced(text: str) -> list[str]:
    """Tags opened and never closed, or closed and never opened.

    The other half of what Telegram refuses a message for — "can't find end of the
    entity" — and cheap to check once the tags are known to be supported.
    """
    stack: list[str] = []
    problems: list[str] = []
    for match in _TAG.finditer(text):
        closing, tag = match.group(1), match.group(2).lower()
        if tag not in SUPPORTED:
            continue  # reported by `unsupported`; not this function's business
        if not closing:
            stack.append(tag)
        elif stack and stack[-1] == tag:
            stack.pop()
        else:
            problems.append(f"</{tag}> with nothing open")
    return problems + [f"<{tag}> never closed" for tag in stack]


def assert_sendable(text: str, *, what: str = "card") -> None:
    """The assertion a card test actually makes.

    Telegram does not truncate or sanitize a message it cannot parse — it **refuses
    the whole thing**, which the caller experiences as silence. So this is checked
    as a property of every rendered card rather than of the fields somebody
    remembered to escape.
    """
    offences = unsupported(text)
    assert not offences, (
        f"{what} contains a '<' Telegram will not parse, so the whole message would "
        f"be refused and the caller would get silence: {offences}. "
        "The fix is escape() at the interpolation, not a narrower field."
    )
    problems = unbalanced(text)
    assert not problems, f"{what} has unbalanced markup Telegram would refuse: {problems}"


__all__ = ["SUPPORTED", "assert_sendable", "unbalanced", "unsupported"]
