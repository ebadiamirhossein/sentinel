"""The 🇮🇷 فارسی button, end to end (M11p).

Driven through the real ``PersianSummariser`` against an ``httpx.MockTransport``,
not a mocked-out summariser: the thing most likely to be wrong here is the join
between "what the model said" and "what we were willing to send", and a double that
returned text directly would test neither half of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from sentinel.analyst.persian.models import PersianSourceKind
from sentinel.analyst.persian.summariser import PersianSummariser
from sentinel.bot.auth import Actor
from sentinel.bot.context import BotContext
from sentinel.bot.handlers import persian
from sentinel.bot.keyboards import PERSIAN_LABEL, PersianCallback, persian_keyboard
from sentinel.bot.persian_cards import (
    CARD_NOT_FOUND,
    COULD_NOT_PRODUCE,
    DAILY_CAP_REACHED,
    NOT_YOUR_CARD,
    REFERENCE_NOTE,
    persian_message,
)
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings, load_config
from sentinel.llm.models import LLMCallKind, LLMCallStatus
from tests.anthropic_double import Recorder, error_response, make_client, message_payload
from tests.bot.telegram_html import assert_sendable
from tests.bot_double import (
    FakeDatabase,
    FakeStore,
    _SignalRow,
    fake_repositories,
    member_account,
    owner_account,
)
from tests.risk_double import account, approved_plan

OWNER = 111
MEMBER = 222
ACTOR = Actor(user_id=OWNER, account=owner_account(OWNER))
MEMBER_ACTOR = Actor(user_id=MEMBER, account=member_account(MEMBER))
SIGNAL_ID = UUID(int=1)
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Vilnius")

#: A plausible answer, and every number in it is on the SOLUSDT card the doubles build.
PERSIAN = (
    "❌ الان نخر — فقط تماشا کن\n"
    "✅ روند چهارساعته هنوز روبه‌بالاست\n"
    "⛔ ترس و طمع روی 74 است و بالای سر قیمت رزیستنس دارد\n"
    "🔑 اگر قیمت به 83.10 برگشت و همان‌جا ماند، اونوقت ورود معنی دارد\n"
    "❌ بسته شدن زیر 81.40 یعنی ایده خراب شده\n"
    "مثل اتوبوسی که داری می‌بینی رد می‌شود — بعدی هم می‌آید."
)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _Chat:
    def __init__(self, chat_id: int = OWNER) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self) -> None:
        self.chat = _Chat()
        self.replies: list[str] = []
        self.edits: list[Any] = []
        self.reply_fail: Exception | None = None

    async def reply(self, text: str, reply_markup: Any = None) -> Any:
        if self.reply_fail is not None:
            raise self.reply_fail
        self.replies.append(text)
        return self

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.edits.append(reply_markup)

    async def edit_text(self, text: str, reply_markup: Any = None) -> None:
        self.edits.append(text)


class FakeQuery:
    def __init__(self, user_id: int = OWNER) -> None:
        self.from_user = type("U", (), {"id": user_id})()
        self.message = FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


def _plan_payload(config: AppConfig) -> dict[str, Any]:
    return approved_plan(config).model_dump(mode="json")


@pytest.fixture(autouse=True)
def _clean_module_state() -> Any:
    """The coalescer and the double-tap window are module state. Reset both between
    tests, or the first test's press silently suppresses the second's."""
    persian._IN_FLIGHT.clear()
    persian._LAST_PRESS.clear()
    yield
    persian._IN_FLIGHT.clear()
    persian._LAST_PRESS.clear()


@pytest.fixture
def store(repo_config: AppConfig) -> FakeStore:
    store = FakeStore()
    store.signals[SIGNAL_ID] = _SignalRow(
        SIGNAL_ID, uuid4(), number=42, user_id=OWNER, plan=_plan_payload(repo_config)
    )
    store.users[OWNER] = owner_account(OWNER)
    store.users[MEMBER] = member_account(MEMBER)
    return store


def build_ctx(
    store: FakeStore,
    responses: list[Any],
    recorder: Recorder | None = None,
    config: AppConfig | None = None,
) -> BotContext:
    from tests.anthropic_double import scripted_transport

    recorder = recorder if recorder is not None else Recorder()
    cfg = config or load_config()
    client = make_client(scripted_transport(responses, recorder), cfg)
    return BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=cfg),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        tz=TZ,
        repositories=fake_repositories(),
        summariser=PersianSummariser(client, cfg),
    )


def ok_response(text: str = PERSIAN) -> dict[str, Any]:
    return message_payload(text, model="claude-sonnet-4-6", input_tokens=900, output_tokens=210)


SIGNAL_PRESS = PersianCallback(source_kind=PersianSourceKind.SIGNAL, source_id=SIGNAL_ID)


async def press(ctx: BotContext, actor: Actor = ACTOR, query: FakeQuery | None = None) -> FakeQuery:
    query = query or FakeQuery(actor.user_id)
    await persian.persian_summary(query, SIGNAL_PRESS, ctx, actor)
    return query


# --------------------------------------------------------------------------- #
# The reference line — on EVERY message this feature can send
# --------------------------------------------------------------------------- #


def test_the_reference_line_says_the_english_card_is_the_reference() -> None:
    """Pinned the way ``formatting.DISCLAIMER`` is. A Persian card is a rewrite by a
    cheaper model of another model's words, with no risk engine behind it — and it is
    trusted MORE than the English one, not less, because it is easier to read."""
    assert REFERENCE_NOTE == "این فقط یک خلاصه ساده است. مرجع اصلی همان کارت انگلیسی بالاست."


@pytest.mark.parametrize(
    "body", [PERSIAN, COULD_NOT_PRODUCE, DAILY_CAP_REACHED, NOT_YOUR_CARD, CARD_NOT_FOUND]
)
def test_every_persian_message_ends_with_the_reference_line(body: str) -> None:
    """Including the failures — those are the ones most likely to be read as the
    system having an opinion."""
    assert persian_message(body).endswith(f"<i>{REFERENCE_NOTE}</i>")


def test_the_model_output_is_escaped_before_it_is_sent() -> None:
    """HANDOFF §4 lesson 2: escape at the value seam. An unescaped ``<`` in a price
    comparison is what made every ``/snapshot`` send fail silently in production, and
    this body is model output rendered with ``parse_mode=HTML``."""
    rendered = persian_message("زیر <200 نرو & مراقب باش")
    assert "&lt;200" in rendered and "&amp;" in rendered
    assert_sendable(rendered, what="persian message")


async def test_a_successful_press_sends_the_summary_under_the_card(store: FakeStore) -> None:
    ctx = build_ctx(store, [ok_response()])
    query = await press(ctx)
    assert query.answers, "Telegram spins forever if a callback is never answered"
    assert len(query.message.replies) == 1
    assert PERSIAN.splitlines()[0] in query.message.replies[0]
    assert query.message.replies[0].endswith(f"<i>{REFERENCE_NOTE}</i>")


async def test_the_original_card_is_never_touched(store: FakeStore) -> None:
    """The button does not disappear and the card does not change: the English card is
    the reference the Persian text points back at, so rewriting it in place would
    remove the thing being pointed at."""
    ctx = build_ctx(store, [ok_response()])
    query = await press(ctx)
    assert query.message.edits == []


# --------------------------------------------------------------------------- #
# The input: the card, and only the card
# --------------------------------------------------------------------------- #


async def test_the_model_is_sent_the_shared_card_and_nothing_else(store: FakeStore) -> None:
    recorder = Recorder()
    ctx = build_ctx(store, [ok_response()], recorder)
    await press(ctx)

    sent = recorder.last
    assert sent["model"] == "claude-sonnet-4-6"
    body = sent["messages"][0]["content"]
    assert "<card_text>" in body and "</card_text>" in body
    # The per-user half of the card is absent, which is what lets two users share one
    # stored text — and what stops one of them reading the other's position size.
    for private in ("Notional", "Margin", "Leverage", "Actual risk", "€", "Signal #"):
        assert private not in body, private
    # And the shared half is there.
    assert "🛑 <b>Stop:" in body and "📊 <b>Thesis</b>" in body


async def test_the_call_is_recorded_as_its_own_kind(store: FakeStore) -> None:
    """So the owner can tell convenience spend from analysis spend by looking, and so
    the cost lands on ``/status`` and ``/pulse`` with no new surface."""
    ctx = build_ctx(store, [ok_response()])
    await press(ctx)
    (call,) = store.llm_calls
    assert call.kind is LLMCallKind.PERSIAN_SUMMARY
    assert call.prompt_version == "persian_summary_v1"
    assert call.model == "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# The numbers rail — fails closed
# --------------------------------------------------------------------------- #


async def test_an_invented_number_is_never_sent(store: FakeStore) -> None:
    """The rail, not the request. If the two cards disagreed about a level, the owner
    would have two systems telling him different things about real money."""
    ctx = build_ctx(store, [ok_response("هدف بعدی 99999.0 است")])
    query = await press(ctx)

    assert query.message.replies == [persian_message(COULD_NOT_PRODUCE)]
    assert store.persian == {}, "a rejected summary must not be cached"


async def test_a_rejected_summary_still_records_what_it_cost(store: FakeStore) -> None:
    """The money was spent. A failure that left no audit row would make the one class
    of call worth reviewing the one class with no trace."""
    ctx = build_ctx(store, [ok_response("هدف بعدی 99999.0 است")])
    await press(ctx)
    assert len(store.llm_calls) == 1
    assert store.llm_calls[0].status is LLMCallStatus.OK


async def test_a_rejection_can_be_retried_because_nothing_was_cached(store: FakeStore) -> None:
    ctx = build_ctx(store, [ok_response("هدف بعدی 99999.0 است"), ok_response()])
    await press(ctx)
    persian._LAST_PRESS.clear()
    query = await press(ctx)
    assert PERSIAN.splitlines()[0] in query.message.replies[0]


async def test_an_api_failure_answers_in_persian_not_in_english(store: FakeStore) -> None:
    ctx = build_ctx(store, [error_response(500)])
    query = await press(ctx)
    assert query.message.replies == [persian_message(COULD_NOT_PRODUCE)]
    assert store.llm_calls[0].status is LLMCallStatus.API_ERROR


async def test_an_unexpected_exception_still_answers_in_persian(store: FakeStore) -> None:
    """``answers_on_failure`` covers message handlers only. A callback that raised
    would leave the reader watching a spinner with no idea why."""

    class _Exploding:
        def __init__(self, session: Any, **kwargs: Any) -> None:
            pass

        async def get(self, signal_id: UUID) -> Any:
            raise RuntimeError("the database fell over")

    built = build_ctx(store, [ok_response()])
    ctx = BotContext(
        settings=built.settings,
        database=built.database,
        clock=built.clock,
        tz=built.tz,
        repositories=replace(built.repositories, signals=_Exploding),  # type: ignore[arg-type]
        summariser=built.summariser,
    )
    query = FakeQuery()
    await persian.persian_summary(query, SIGNAL_PRESS, ctx, ACTOR)
    assert query.message.replies == [persian_message(COULD_NOT_PRODUCE)]


# --------------------------------------------------------------------------- #
# Caching, coalescing and the double tap
# --------------------------------------------------------------------------- #


async def test_a_second_press_returns_the_stored_text_and_buys_nothing(
    store: FakeStore,
) -> None:
    recorder = Recorder()
    ctx = build_ctx(store, [ok_response()], recorder)
    await press(ctx)
    persian._LAST_PRESS.clear()
    second = await press(ctx)

    assert len(recorder) == 1, "the second press must not buy a second call"
    assert second.message.replies == [persian_message(PERSIAN)]


async def test_two_users_reading_the_same_card_read_the_same_words(store: FakeStore) -> None:
    """The point of keying on a hash of the card text rather than on a signal id: the
    two are different rows in ``signals``, and the same card."""
    store.signals[SIGNAL_ID].user_id = OWNER
    recorder = Recorder()
    ctx = build_ctx(store, [ok_response()], recorder)
    mine = await press(ctx)

    # The member's own row, same plan, same shared card.
    other_id = UUID(int=2)
    store.signals[other_id] = _SignalRow(
        other_id, uuid4(), number=99, user_id=MEMBER, plan=store.signals[SIGNAL_ID].plan
    )
    theirs = FakeQuery(MEMBER)
    await persian.persian_summary(
        theirs,
        PersianCallback(source_kind=PersianSourceKind.SIGNAL, source_id=other_id),
        ctx,
        MEMBER_ACTOR,
    )

    assert len(recorder) == 1, "one card, one paid call"
    assert theirs.message.replies == mine.message.replies


async def test_a_double_tap_within_the_window_sends_nothing_twice(store: FakeStore) -> None:
    ctx = build_ctx(store, [ok_response()])
    await press(ctx)
    again = await press(ctx)  # immediately, inside double_tap_seconds
    assert again.message.replies == []
    assert again.answers, "the query is still answered, so the spinner stops"


async def test_two_simultaneous_presses_buy_one_call(store: FakeStore) -> None:
    """The in-flight coalescer. Both presses miss the cache, so without it both would
    pay — and the loser's money is already spent by the time ``ON CONFLICT`` fires."""
    recorder = Recorder()
    ctx = build_ctx(store, [ok_response()], recorder)
    first, second = FakeQuery(OWNER), FakeQuery(MEMBER)
    store.signals[SIGNAL_ID].user_id = OWNER
    other_id = UUID(int=3)
    store.signals[other_id] = _SignalRow(
        other_id, uuid4(), number=7, user_id=MEMBER, plan=store.signals[SIGNAL_ID].plan
    )
    await asyncio.gather(
        persian.persian_summary(first, SIGNAL_PRESS, ctx, ACTOR),
        persian.persian_summary(
            second,
            PersianCallback(source_kind=PersianSourceKind.SIGNAL, source_id=other_id),
            ctx,
            MEMBER_ACTOR,
        ),
    )
    assert len(recorder) == 1
    assert first.message.replies == second.message.replies


# --------------------------------------------------------------------------- #
# Authorization and missing cards
# --------------------------------------------------------------------------- #


async def test_another_users_card_is_refused(store: FakeStore) -> None:
    """A forwarded card carries its buttons to whoever it was forwarded to."""
    ctx = build_ctx(store, [ok_response()])
    query = FakeQuery(MEMBER)
    await persian.persian_summary(query, SIGNAL_PRESS, ctx, MEMBER_ACTOR)
    assert NOT_YOUR_CARD in query.answers
    assert query.message.replies == []


async def test_a_missing_card_says_so_in_persian(store: FakeStore) -> None:
    ctx = build_ctx(store, [ok_response()])
    query = FakeQuery()
    await persian.persian_summary(
        query,
        PersianCallback(source_kind=PersianSourceKind.SIGNAL, source_id=UUID(int=99)),
        ctx,
        ACTOR,
    )
    assert query.message.replies == [persian_message(CARD_NOT_FOUND)]


async def test_a_deployment_without_a_key_degrades_explicitly(store: FakeStore) -> None:
    ctx = build_ctx(store, [ok_response()])
    ctx = BotContext(
        settings=ctx.settings,
        database=ctx.database,
        clock=ctx.clock,
        tz=ctx.tz,
        repositories=ctx.repositories,
        summariser=None,
    )
    query = await press(ctx)
    assert query.message.replies == [persian_message(COULD_NOT_PRODUCE)]


# --------------------------------------------------------------------------- #
# The spend rails
# --------------------------------------------------------------------------- #


async def test_a_user_past_their_daily_generation_cap_is_refused(
    store: FakeStore, repo_config: AppConfig
) -> None:
    tight = repo_config.model_copy(
        update={
            "persian_summary": repo_config.persian_summary.model_copy(
                update={"daily_generations_per_user": 1}
            )
        }
    )
    recorder = Recorder()
    ctx = build_ctx(store, [ok_response(), ok_response("چیز دیگری")], recorder, config=tight)
    await press(ctx)

    # A genuinely different card, so the cache cannot answer it. Sized at €200 rather
    # than the default €10,000: the engine returns a different plan at that capital,
    # not the same plan scaled, so the shared form differs too.
    other_id = UUID(int=4)
    store.signals[other_id] = _SignalRow(
        other_id,
        uuid4(),
        number=8,
        user_id=OWNER,
        plan=approved_plan(repo_config, account=account(capital_eur="200")).model_dump(mode="json"),
    )
    persian._LAST_PRESS.clear()
    query = FakeQuery()
    await persian.persian_summary(
        query,
        PersianCallback(source_kind=PersianSourceKind.SIGNAL, source_id=other_id),
        ctx,
        ACTOR,
    )
    assert query.message.replies == [persian_message(DAILY_CAP_REACHED)]
    assert len(recorder) == 1, "the cap is checked BEFORE the call, not after it"


async def test_the_deployment_wide_usd_cap_stops_generation(
    store: FakeStore, repo_config: AppConfig
) -> None:
    """The rail that protects forex. Crypto's reserved floor holds money against other
    *markets*; forex has no floor at all and only ~$3 of slack under the ceiling."""
    tight = repo_config.model_copy(
        update={
            "persian_summary": repo_config.persian_summary.model_copy(
                update={"daily_usd_cap": Decimal("0")}
            )
        }
    )
    recorder = Recorder()
    ctx = build_ctx(store, [ok_response()], recorder, config=tight)
    query = await press(ctx)
    assert query.message.replies == [persian_message(DAILY_CAP_REACHED)]
    assert len(recorder) == 0


# --------------------------------------------------------------------------- #
# The button itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", list(PersianSourceKind))
def test_the_callback_payload_fits_telegrams_limit(kind: PersianSourceKind) -> None:
    packed = PersianCallback(source_kind=kind, source_id=uuid4()).pack()
    assert len(packed.encode("utf-8")) <= 64, packed


def test_the_pulse_keyboard_is_one_persian_row() -> None:
    keyboard = persian_keyboard(PersianSourceKind.PULSE_VERDICT, uuid4())
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [PERSIAN_LABEL]
