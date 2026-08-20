"""``/help`` — specs/TELEGRAM_UX.md §3, written for somebody who does not read specs.

The point of these assertions is not that the words are present but that the
*concepts* are: a card shows capital, a risk %, a derived leverage, a laddered
entry, a stop, an invalidation, three targets, net-and-gross R and a cost line, and
three buttons whose distinction decides which statistics an outcome joins. A help
text that left any of those undefined would leave somebody guessing at a number
they are about to place an order against.
"""

from __future__ import annotations

import re

import pytest

from sentinel.bot.cards import HELP_LINES, MESSAGE_LIMIT, help_card

#: The "(1/2)" footer ``cards._pages`` appends once there is more than one page. It
#: is delivery, not content, so it comes off before the text is asserted on.
PAGE_MARKER = re.compile(r"\n\n<i>\(\d+/\d+\)</i>\Z")

#: The two sentences that must survive any future edit, in substance.
NEVER_TRADES = "never trades"
PLACE_IT_YOURSELF = "you place every order yourself"


def body() -> str:
    """Every page as one string — what a reader ends up having read.

    ``/help`` outgrew a single Telegram message at M8.6 and now splits rather than
    shortens (M8.5's ruling), so the assertions below are about the *text*, which is
    what they were always about; only the delivery changed.
    """
    return "\n".join(PAGE_MARKER.sub("", page) for page in help_card())


def test_every_page_of_help_fits_a_telegram_message() -> None:
    """4096 is a hard refusal, not a truncation: an over-long page is not delivered
    at all. The pages are cut to a lower budget so the "(1/2)" marker, added after
    the cut, cannot push one back over."""
    assert all(len(page) < MESSAGE_LIMIT for page in help_card())


def test_help_splits_rather_than_dropping_a_line() -> None:
    """The pages, rejoined, are the source lines — nothing was lost in the split.

    Asserted line-for-line rather than by page count, because a paginator that cut
    correctly and dropped the tail would pass any weaker check, and the tail of
    /help is the disclaimer.
    """
    assert body() == "\n".join(HELP_LINES)


@pytest.mark.parametrize(
    "concept",
    [
        "capital",
        "risk %",
        "leverage",
        "entry ladder",
        "stop",
        "invalidation",
        "targets",
        "net",
        "gross",
        "fees",
        "funding",
        "taken",
        "watching",
        "skip",
        "real",
        "hypothetical",
    ],
)
def test_every_term_a_card_uses_is_explained(concept: str) -> None:
    assert concept in body().lower(), f"/help never mentions {concept!r}"


def test_the_two_things_that_must_never_be_misunderstood_are_both_there() -> None:
    """That nothing is traded automatically, and that the human places the order."""
    text = body().lower()
    assert NEVER_TRADES in text
    assert PLACE_IT_YOURSELF in text


def test_it_says_so_before_anything_else() -> None:
    """Somebody who reads only the first screen must still learn the one fact that
    matters: this system does not touch their exchange account."""
    first_screen = "\n".join(HELP_LINES[:4]).lower()
    assert NEVER_TRADES in first_screen


def test_leverage_is_explained_as_margin_rather_than_as_risk() -> None:
    """The single most dangerous misreading on a card. Leverage here is derived,
    capped, and reduced until liquidation sits beyond the stop — it changes the
    margin posted, never the loss taken."""
    text = body().lower()
    assert "not how much you can lose" in text
    assert "liquidation" in text


def test_it_states_that_the_win_rate_is_not_yet_measured() -> None:
    """The same claim the first-run acknowledgement makes. Somebody who accepted it
    weeks ago and then reads /help must not find a more confident story."""
    assert "not yet measured" in body().lower()


def test_it_carries_the_standing_disclaimer() -> None:
    from sentinel.bot.formatting import DISCLAIMER

    assert DISCLAIMER in body()


def test_it_explains_why_the_three_buttons_are_not_interchangeable() -> None:
    """Taken feeds the real statistics and spends the risk budget; the other two do
    not. That distinction is the whole reason the measured win rate means anything."""
    text = body().lower()
    assert "real" in text and "hypothetical" in text
    assert "open-risk budget" in text


def test_the_help_text_is_a_tuple_of_lines_and_not_built_by_arithmetic() -> None:
    """``cards.py`` is under the layer-1 AST scan (no ``+`` anywhere, strings
    included), so the text has to be joined rather than concatenated. This asserts
    the shape that keeps that true."""
    assert isinstance(HELP_LINES, tuple)
    assert body() == "\n".join(HELP_LINES)


# --------------------------------------------------------------------------- #
# M8.6 — the two commands added at M8.6 are explained, not merely registered
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "concept",
    [
        "/journal",
        "/snapshot",
        "spreadsheet",
        "separate sheets",
        "yours alone",
        "no model involved",
        "support and resistance",
    ],
)
def test_the_m8_6_commands_are_explained_in_words(concept: str) -> None:
    """A command on the ``/`` menu that ``/help`` never mentions is a command
    nobody finds. Both of these produce something unlike every other reply in the
    bot — a file, and a wall of raw indicators — so what they are for has to be
    said, not just listed."""
    assert concept in body().lower(), f"/help never mentions {concept!r}"


def test_help_names_the_journal_boundary() -> None:
    """The one thing about ``/journal`` a member has to be able to rely on: the file
    is theirs and holds nobody else's rows. It is asserted in ``/help`` as well as on
    the caption, because the caption is gone the moment the file is forwarded."""
    assert "nobody else's rows" in body().lower()
