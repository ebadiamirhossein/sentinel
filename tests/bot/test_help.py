"""``/help`` — specs/TELEGRAM_UX.md §3, written for somebody who does not read specs.

The point of these assertions is not that the words are present but that the
*concepts* are: a card shows capital, a risk %, a derived leverage, a laddered
entry, a stop, an invalidation, three targets, net-and-gross R and a cost line, and
three buttons whose distinction decides which statistics an outcome joins. A help
text that left any of those undefined would leave somebody guessing at a number
they are about to place an order against.
"""

from __future__ import annotations

import pytest

from sentinel.bot.cards import HELP_LINES, help_card

#: The two sentences that must survive any future edit, in substance.
NEVER_TRADES = "never trades"
PLACE_IT_YOURSELF = "you place every order yourself"


def test_help_fits_one_telegram_message() -> None:
    """4096 is the hard cap. A truncated /help would cut off mid-explanation."""
    assert len(help_card()) < 4096


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
    assert concept in help_card().lower(), f"/help never mentions {concept!r}"


def test_the_two_things_that_must_never_be_misunderstood_are_both_there() -> None:
    """That nothing is traded automatically, and that the human places the order."""
    body = help_card().lower()
    assert NEVER_TRADES in body
    assert PLACE_IT_YOURSELF in body


def test_it_says_so_before_anything_else() -> None:
    """Somebody who reads only the first screen must still learn the one fact that
    matters: this system does not touch their exchange account."""
    first_screen = "\n".join(HELP_LINES[:4]).lower()
    assert NEVER_TRADES in first_screen


def test_leverage_is_explained_as_margin_rather_than_as_risk() -> None:
    """The single most dangerous misreading on a card. Leverage here is derived,
    capped, and reduced until liquidation sits beyond the stop — it changes the
    margin posted, never the loss taken."""
    body = help_card().lower()
    assert "not how much you can lose" in body
    assert "liquidation" in body


def test_it_states_that_the_win_rate_is_not_yet_measured() -> None:
    """The same claim the first-run acknowledgement makes. Somebody who accepted it
    weeks ago and then reads /help must not find a more confident story."""
    assert "not yet measured" in help_card().lower()


def test_it_carries_the_standing_disclaimer() -> None:
    from sentinel.bot.formatting import DISCLAIMER

    assert DISCLAIMER in help_card()


def test_it_explains_why_the_three_buttons_are_not_interchangeable() -> None:
    """Taken feeds the real statistics and spends the risk budget; the other two do
    not. That distinction is the whole reason the measured win rate means anything."""
    body = help_card().lower()
    assert "real" in body and "hypothetical" in body
    assert "open-risk budget" in body


def test_the_help_text_is_a_tuple_of_lines_and_not_built_by_arithmetic() -> None:
    """``cards.py`` is under the layer-1 AST scan (no ``+`` anywhere, strings
    included), so the text has to be joined rather than concatenated. This asserts
    the shape that keeps that true."""
    assert isinstance(HELP_LINES, tuple)
    assert help_card() == "\n".join(HELP_LINES)
