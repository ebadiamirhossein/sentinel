"""The numbers rail (M11p §B). If this file is wrong, two systems can tell the owner
different things about a stop price, which is the failure this feature exists to make
impossible.

The tests are written against the **real golden card**, not a hand-written string: the
tokens a card actually carries -- ``0.049``, ``64,150``-style grouping, ``EMA200``,
percentages, R multiples, a euro amount -- are the input the check has to survive, and
a fixture invented to suit the check would prove nothing about them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.analyst.persian.numbers import (
    canonical,
    check_numbers,
    numeric_tokens,
    plain_text,
)

CARD = (Path(__file__).resolve().parents[1] / "fixtures" / "golden_cycle" / "card.txt").read_text(
    encoding="utf-8"
)


# ── tokenising: one regex, both sides ───────────────────────────────────────


def test_markup_does_not_change_the_tokens() -> None:
    """``<b>64150.0</b>`` and ``64150.0`` must tokenise identically, or every bolded
    price on every card reads as a different number from the same price in prose."""
    assert numeric_tokens("<b>64150.0</b>") == numeric_tokens("64150.0") == ["64150.0"]


def test_a_number_glued_to_a_word_yields_the_same_token_as_a_spaced_one() -> None:
    """``EMA200`` on the card and ``EMA 200`` in the summary are the same claim.

    This is the whole reason both sides go through one tokeniser rather than a
    substring search: a substring check would pass ``EMA 200`` only by accident and
    reject it whenever the card happened to glue the number on.
    """
    assert numeric_tokens("EMA200 above EMA50") == numeric_tokens("EMA 200 above EMA 50")


def test_entities_are_restored_before_tokenising() -> None:
    assert plain_text("stop &lt;63450.0") == "stop <63450.0"


# ── the rule: output numbers must be on the card ────────────────────────────


def test_a_number_copied_from_the_card_passes() -> None:
    assert check_numbers(card=CARD, summary="استاپ روی 63450.0 است").ok


def test_the_summary_may_drop_numbers_the_card_carries() -> None:
    """Containment is one-directional on purpose -- the summary is a short rewrite."""
    assert check_numbers(card=CARD, summary="فعلا صبر کن.").ok


def test_an_invented_number_fails() -> None:
    check = check_numbers(card=CARD, summary="هدف بعدی 71000.0 است")
    assert not check.ok
    assert check.foreign_tokens == ("71000.0",)


def test_the_check_would_catch_a_changed_value() -> None:
    """The proof of teeth, built the way HANDOFF §4 item 13 says to build one.

    The card's stop is ``63450.0``. This perturbs it by **one digit at the last place**
    -- the same magnitude, the same scale, the same number of characters -- so it fails
    only if the comparison genuinely works. A mutation that changed the number beyond
    recognition would pass against a check that had quietly stopped checking.
    """
    assert "63450.0" in CARD
    assert not check_numbers(card=CARD, summary="استاپ روی 63450.1 است").ok


def test_a_rounded_number_fails() -> None:
    """``64150.0`` -> ``64150`` is a reformat, and reformats are forbidden: the printed
    precision of a stop is a claim about how exact the level is."""
    assert "64150.0" in CARD
    assert not check_numbers(card=CARD, summary="ورود 64150").ok


def test_a_recomputed_percentage_fails() -> None:
    """The card says ``-0.86%``. Nothing may derive ``0.9`` from it."""
    assert not check_numbers(card=CARD, summary="استاپ حدود 0.9% پایین تر است").ok


# ── grouping marks are normalised; precision is not ─────────────────────────


@pytest.mark.parametrize(
    ("written", "expected"),
    [("64,150.0", "64150.0"), ("1,234,567", "1234567"), ("64150.0", "64150.0")],
)
def test_thousands_separators_are_a_grouping_mark_not_a_value(written: str, expected: str) -> None:
    """Removing a grouping mark cannot change what the number is, so it is normalised
    away. Dropping a trailing zero can, so it is not -- see :func:`canonical`."""
    assert canonical(written) == expected


def test_a_grouped_number_matches_its_ungrouped_form_in_the_card() -> None:
    assert check_numbers(card="stop 64,150.0", summary="استاپ 64150.0").ok
    assert check_numbers(card="stop 64150.0", summary="استاپ 64,150.0").ok


def test_a_decimal_comma_is_not_a_grouping_mark_and_fails() -> None:
    """``64150,0`` canonicalises to ``641500``, which is on no card. Failing closed on
    a European decimal comma is correct: it is a reformat of the printed number."""
    assert not check_numbers(card="stop 64150.0", summary="استاپ 64150,0").ok


# ── ASCII digits only ───────────────────────────────────────────────────────


def test_persian_digits_fail_even_when_the_value_is_right() -> None:
    """``۶۳۴۵۰`` *is* the card's stop. It still fails: the rule is that numbers are
    copied character-for-character, and this is not that. The check agrees with the
    prompt rather than quietly widening it."""
    check = check_numbers(card=CARD, summary="استاپ روی ۶۳۴۵۰ است")
    assert not check.ok
    assert check.non_ascii_digits


def test_arabic_indic_digits_fail_too() -> None:
    assert not check_numbers(card=CARD, summary="استاپ ٦٣٤٥٠").ok


def test_persian_number_words_are_not_digits_and_pass() -> None:
    """The prompt tells the model to count in words precisely so that counting is not a
    reason to write a digit the card does not carry."""
    assert check_numbers(card=CARD, summary="دو دلیل برای صبر کردن وجود دارد.").ok


# ── what the log will say ───────────────────────────────────────────────────


def test_detail_names_which_rule_broke() -> None:
    assert "not on the card" in check_numbers(card="a 1", summary="b 2").detail
    assert "non-ASCII" in check_numbers(card="a 1", summary="۲").detail
    assert "every number" in check_numbers(card="a 1", summary="a 1").detail
