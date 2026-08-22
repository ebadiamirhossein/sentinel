"""Both Persian rails (M11p §B and the verdict rail). If this file is wrong, two
systems can tell the owner different things about a stop price — or about whether he
should be buying at all — which is the failure this feature exists to make impossible.

The tests are written against the **real golden card**, not a hand-written string: the
tokens a card actually carries -- ``0.049``, ``64,150``-style grouping, ``EMA200``,
percentages, R multiples, a euro amount -- are the input the check has to survive, and
a fixture invented to suit the check would prove nothing about them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.analyst.persian.numbers import (
    VERDICT_MARKERS,
    VerdictClass,
    canonical,
    check_numbers,
    check_verdict,
    numeric_tokens,
    plain_text,
    verdict_class,
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


# ── the verdict rail (M11p, owner requirement H1) ───────────────────────────
#
# The numbers rail above is structurally blind to a WATCHLIST rewritten as an
# encouraging card, because softening a verdict invents no number. This is the other
# rail, and on `/pulse SYMBOL` -- the only surface where a non-CANDIDATE verdict is
# reachable at all -- it is very nearly the only rail there is: of the fifteen numeric
# tokens on that card, six are parts of a date, two are confidence and four are
# timeframe labels.

#: **Real model output.** `claude-sonnet-4-6`, `persian_summary_v1`, 2026-08-22, from
#: one live call on a rendered WATCHLIST `/pulse SOLUSDT` card — 389 in / 373 out,
#: 9.7 s, both rails PASS. Kept verbatim so the teeth test below alters something the
#: model actually wrote rather than something a test author invented to be caught.
REAL_WATCHLIST_SUMMARY = """❌ الان نخر — فقط تماشا کن

👀 چی خوبه
- روند 4 ساعته هنوز صعودیه
- قیمت داره به EMA50 و سطح بریک‌اوت 82.4 پولبک می‌زنه
- Open Interest داره با قیمت بالا میره

⛔ چرا الان نه
- شاخص ترس و طمع روی 74 (طمع) — یعنی بازار شلوغه و ریسک ازدحام لانگ بالاست
- رزیستنس مهم روی 84.6 نزدیکه

🔑 شرط ورود
اگر قیمت بالای 82.4 تثبیت شد و شرایط بهتر شد، اونوقت می‌شه دنبال پلن گشت.

❌ چی خرابش می‌کنه
کندل 1 ساعته زیر 81.40 بسته بشه — ایده تموم‌شده.

فعلاً این سهم مثل ماشینیه که موتورش گرم‌شده ولی هنوز چراغ سبز نداره — صبر کن."""


def test_the_real_watchlist_summary_passes() -> None:
    """The baseline. Without this, the teeth test below could pass because the rail
    rejects everything."""
    assert check_verdict(summary=REAL_WATCHLIST_SUMMARY, candidate_status="WATCHLIST").ok


def test_the_verdict_rail_would_catch_a_watchlist_rewritten_as_a_buy() -> None:
    """**The proof of teeth**, and it alters real model output rather than a fixture
    written to be caught.

    One line changes — the opening verdict — from "don't buy, just watch" to "conditions
    are good, you can go in". Every other word, and every number, is untouched: the
    numbers rail passes it, which is the point. Softening a verdict invents no number,
    so the rail that counts numbers cannot see this and never could.
    """
    softened = REAL_WATCHLIST_SUMMARY.replace(
        "❌ الان نخر — فقط تماشا کن", "✅ شرایط خوبه — می‌تونی وارد شی"
    )
    assert softened != REAL_WATCHLIST_SUMMARY, "the substitution did not apply"

    # The numbers rail is untroubled by it, and this is the sharpest way to say so:
    # the alteration changed NO NUMBER AT ALL, so no rail built on numbers can see it,
    # whatever card it is checked against.
    assert numeric_tokens(softened) == numeric_tokens(REAL_WATCHLIST_SUMMARY)

    check = check_verdict(summary=softened, candidate_status="WATCHLIST")
    assert not check.ok
    assert "does not start with" in check.detail


def test_a_candidate_card_described_discouragingly_is_also_caught() -> None:
    """The mirror. Safe-direction, and still two systems disagreeing about one setup."""
    assert not check_verdict(summary="❌ الان نخر", candidate_status="CANDIDATE").ok


@pytest.mark.parametrize("status", ["WATCHLIST", "NO_SETUP", "watchlist", "something_new"])
def test_everything_that_is_not_a_candidate_is_not_actionable(status: str) -> None:
    """Including a status this rail has never heard of. A verdict it cannot classify
    must not be allowed to read as a buy — the safe direction is the default."""
    assert verdict_class(status) is VerdictClass.NOT_YET
    assert check_verdict(summary="❌ صبر کن", candidate_status=status).ok
    assert not check_verdict(summary="✅ بخر", candidate_status=status).ok


def test_the_two_marker_sets_are_disjoint() -> None:
    """Structural, so the vocabulary cannot drift into a marker that means both."""
    actionable = VERDICT_MARKERS[VerdictClass.ACTIONABLE]
    not_yet = VERDICT_MARKERS[VerdictClass.NOT_YET]
    assert actionable and not_yet
    assert not (actionable & not_yet)


def test_a_heading_before_the_verdict_line_fails() -> None:
    """The prompt says nothing may come before the marker. A summary that buried its
    verdict under a title would be one this rail could not read."""
    assert not check_verdict(summary="خلاصه سیگنال\n\n❌ الان نخر", candidate_status="WATCHLIST").ok


def test_leading_blank_lines_are_not_a_heading() -> None:
    """Whitespace is not content. The rail reads the first NON-EMPTY line, so a model
    that starts with a newline is not punished for formatting."""
    assert check_verdict(summary="\n\n  ❌ الان نخر", candidate_status="WATCHLIST").ok


def test_the_rail_checks_the_marker_and_not_the_meaning() -> None:
    """Stated as a test so the limit is recorded where somebody will read it.

    ``❌ بخر`` — "don't-buy marker, buy text" — PASSES. Catching that needs a phrase
    vocabulary, and a phrase vocabulary is where false failures live: Persian has many
    good ways to say "do not buy" and a fixed list rejects the ones nobody thought of.
    A rail that fires wrongly on a true summary is worse than a documented gap.
    """
    assert check_verdict(summary="❌ بخر", candidate_status="WATCHLIST").ok
