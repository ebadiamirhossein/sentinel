"""Persian user-facing text for the M11p summary button.

Every string a Persian summary can produce lives here, for the same reason
``formatting.DISCLAIMER`` lives in one place: a message the owner reads in the
language he trusts most is the last message that should be assembled ad hoc at three
call sites.

**The reference line is the point of this module.** A Persian summary is a rewrite, by
a cheaper model, of another model's words. There is no risk engine behind it. If it
ever disagrees with the English card, the English card wins -- it is the money one, and
it is the one ``sentinel/risk/`` produced. A friendlier card in your own language is
trusted *more*, not less, which is exactly why it has to point back at the authority.

So the line is appended **by this module, after the model returns**, and is never
something the prompt asks the model to write. A rail the model can decline to emit is
not a rail. It goes on the failure messages too -- those are the ones most likely to be
read as the system having an opinion.
"""

from __future__ import annotations

from sentinel.bot.formatting import escape

#: Appended to every Persian message this feature can send, without exception.
#: Pinned by ``tests/bot/test_persian_summary.py`` the way the English disclaimer is.
REFERENCE_NOTE = "این فقط یک خلاصه ساده است. مرجع اصلی همان کارت انگلیسی بالاست."

#: The generic failure. Deliberately says nothing about *why*: the causes are an API
#: error, a spend cap and a failed numbers check, and none of the three is actionable
#: by the reader. Never silence, and never an English traceback.
COULD_NOT_PRODUCE = "نتونستم خلاصه فارسی رو بسازم. کمی بعد دوباره امتحان کن."

#: The one failure that *is* actionable, so it gets its own words.
DAILY_CAP_REACHED = "سقف خلاصه های فارسی امروز پر شده. فردا دوباره امتحان کن."

#: A forwarded card carries its buttons to whoever it was forwarded to.
NOT_YOUR_CARD = "این کارت مال تو نیست."

#: The card the button refers to is gone -- deleted, or from a database that was reset.
CARD_NOT_FOUND = "این کارت دیگه توی دیتابیس نیست."


def persian_message(body: str) -> str:
    """One Persian message: the body, escaped, above the reference line.

    ``escape`` is not optional. The body is model output rendered with
    ``parse_mode=HTML``, and an unescaped ``<`` in a price comparison is the exact
    defect that made every ``/snapshot`` send fail silently in production.
    """
    return f"{escape(body).strip()}\n\n<i>{REFERENCE_NOTE}</i>"


__all__ = [
    "CARD_NOT_FOUND",
    "COULD_NOT_PRODUCE",
    "DAILY_CAP_REACHED",
    "NOT_YOUR_CARD",
    "REFERENCE_NOTE",
    "persian_message",
]
