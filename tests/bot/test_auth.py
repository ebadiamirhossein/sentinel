"""The authorization gate — specs/TELEGRAM_UX.md §1 and §7 (M8.1).

This replaces ``test_allowlist.py``. Its guarantee is kept and narrowed rather than
dropped: **silence is still literal**, and it is still asserted as *zero* outbound
calls rather than as a refusal. What changed is that exactly one update — ``/start``
from an unknown id — is now answered, because a system somebody has to be able to
join cannot answer nothing at all.

The centre of this file is the truth table and its meta-test. This is the gate on
other people's money and on the privacy of their books, so "which combinations did
we think of" must be a list nobody can silently leave a hole in.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import product
from typing import Any
from uuid import uuid4

import pytest

from sentinel.bot.auth import (
    TABLE,
    Actor,
    AuthMiddleware,
    OwnerOnly,
    Standing,
    UpdateKind,
    Verdict,
    classify,
    standing_of,
)
from sentinel.bot.context import BotContext
from sentinel.bot.keyboards import AckCallback, DecisionCallback
from sentinel.bot.models import ACK_VERSION, SignalDecision, UserStatus
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from tests.bot_double import (
    ACCOUNT_NOW,
    FakeDatabase,
    FakeStore,
    fake_repositories,
    member_account,
    owner_account,
)

OWNER = 111
MEMBER = 222
STRANGER = 333


class _User:
    def __init__(self, user_id: int, username: str | None = "someone") -> None:
        self.id = user_id
        self.username = username
        self.full_name = "Some One"


class _Chat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    """Enough of ``aiogram.types.Message`` for the gate: text, and ``answer``."""

    def __init__(self, text: str = "/status", user_id: int = MEMBER) -> None:
        self.text = text
        self.from_user = _User(user_id)
        self.chat = _Chat(user_id)
        self.replies: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.replies.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    """Enough of ``aiogram.types.CallbackQuery``."""

    def __init__(self, data: str, user_id: int = MEMBER) -> None:
        self.data = data
        self.from_user = _User(user_id)
        self.message = None
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def ctx(store: FakeStore, tz: Any, clock: FrozenClock) -> BotContext:
    settings = Settings(secrets=Secrets(_env_file=None), config=load_config())
    return BotContext(
        settings=settings,
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=clock,
        tz=tz,
        repositories=fake_repositories(),
    )


async def run(middleware: AuthMiddleware, event: Any) -> Actor | None:
    """Returns the injected actor when the handler ran, else ``None``."""
    seen: dict[str, Actor] = {}

    async def handler(_: Any, data: dict[str, Any]) -> None:
        seen["actor"] = data["actor"]

    await middleware(handler, event, {"event_from_user": event.from_user})
    return seen.get("actor")


# --------------------------------------------------------------------------- #
# The truth table
# --------------------------------------------------------------------------- #


def test_the_table_covers_every_standing_and_every_update_kind() -> None:
    """No cell may be missing.

    A ``KeyError`` at runtime in the gate would be an unhandled exception on a
    Telegram update — which aiogram logs and swallows, i.e. the update would be
    dropped silently for a reason nobody chose. Adding a ``Standing`` without
    deciding its four verdicts is exactly the mistake a hand-written list of
    "interesting cases" cannot catch, because nothing tells you what you forgot.
    """
    assert set(TABLE) == set(product(Standing, UpdateKind))


def test_only_start_ever_gets_a_reply_from_a_non_active_caller() -> None:
    """The narrowed §1 promise, stated as a property over the whole table.

    Everything that is not ``PASS`` for an ACTIVE or UNACKNOWLEDGED caller is either
    silence or a reply to ``/start``. Nothing else in any state may produce outbound
    traffic, because a reply confirms to a stranger that they found a live private
    bot that talks about somebody's capital.
    """
    speaking = {Verdict.NOTICE, Verdict.ACK_REQUIRED}
    for (standing, kind), verdict in TABLE.items():
        if verdict in speaking and standing not in {Standing.UNACKNOWLEDGED, Standing.ACTIVE}:
            assert kind is UpdateKind.START, f"{standing}/{kind} answers a non-/start update"


def test_a_stranger_may_only_start() -> None:
    assert TABLE[(Standing.UNKNOWN, UpdateKind.START)] is Verdict.PASS
    for kind in (UpdateKind.HELP, UpdateKind.ACK, UpdateKind.OTHER):
        assert TABLE[(Standing.UNKNOWN, kind)] is Verdict.DROP


@pytest.mark.parametrize(
    "status", [UserStatus.PENDING, UserStatus.REJECTED, UserStatus.SUSPENDED, UserStatus.LEFT]
)
def test_every_non_approved_standing_is_told_once_and_otherwise_silent(
    status: UserStatus,
) -> None:
    standing = Standing(status.value)
    assert TABLE[(standing, UpdateKind.START)] is Verdict.NOTICE
    for kind in (UpdateKind.HELP, UpdateKind.ACK, UpdateKind.OTHER):
        assert TABLE[(standing, kind)] is Verdict.DROP


def test_an_unacknowledged_user_can_still_read_help_and_press_the_button() -> None:
    """They are being asked to accept that this is experimental and unmeasured.
    Refusing to explain what the numbers mean until after they agree is backwards."""
    assert TABLE[(Standing.UNACKNOWLEDGED, UpdateKind.HELP)] is Verdict.PASS
    assert TABLE[(Standing.UNACKNOWLEDGED, UpdateKind.ACK)] is Verdict.PASS
    assert TABLE[(Standing.UNACKNOWLEDGED, UpdateKind.OTHER)] is Verdict.ACK_REQUIRED


# --------------------------------------------------------------------------- #
# standing_of and classify
# --------------------------------------------------------------------------- #


def test_standing_of_a_missing_row_is_unknown() -> None:
    assert standing_of(None) is Standing.UNKNOWN


def test_an_approved_but_unacknowledged_account_is_not_active() -> None:
    assert standing_of(owner_account(acknowledged=False)) is Standing.UNACKNOWLEDGED
    assert standing_of(owner_account()) is Standing.ACTIVE


def test_a_superseded_acknowledgement_asks_again() -> None:
    """The version is part of the record, so bumping it re-gates everybody.

    Accepting "experimental, unmeasured, you can lose money" is not a one-time
    formality if the wording materially changes — and a stored timestamp with no
    version would make it impossible to tell which words were agreed to.
    """
    stale = owner_account().model_copy(update={"acknowledged_version": "v0"})
    assert standing_of(stale) is Standing.UNACKNOWLEDGED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", UpdateKind.START),
        ("/start@zyndix_trader_bot", UpdateKind.START),
        ("/START", UpdateKind.START),
        ("/help", UpdateKind.HELP),
        ("/help me", UpdateKind.HELP),
        ("/capital 10000", UpdateKind.OTHER),
        ("hello", UpdateKind.OTHER),
        ("/startle", UpdateKind.OTHER),
    ],
)
def test_classify_reads_the_command_word_only(text: str, expected: UpdateKind) -> None:
    """Matched on the text because the gate runs *before* routing — the whole point
    is that an unauthorized update never reaches a router."""
    assert classify(FakeMessage(text)) is expected  # type: ignore[arg-type]


def test_the_acknowledgement_button_is_recognised_before_routing() -> None:
    ack = AckCallback(version=ACK_VERSION).pack()
    other = DecisionCallback(signal_id=uuid4(), decision=SignalDecision.TAKEN).pack()
    assert classify(FakeQuery(ack)) is UpdateKind.ACK  # type: ignore[arg-type]
    assert classify(FakeQuery(other)) is UpdateKind.OTHER  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The middleware, end to end against the fake store
# --------------------------------------------------------------------------- #


async def test_an_active_user_passes_with_their_account_attached(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER)
    actor = await run(AuthMiddleware(ctx), FakeMessage("/stats", MEMBER))
    assert actor is not None
    assert actor.known().telegram_user_id == MEMBER
    assert actor.is_owner is False


async def test_the_owner_is_recognised_as_one(ctx: BotContext, store: FakeStore) -> None:
    store.users[OWNER] = owner_account(OWNER)
    actor = await run(AuthMiddleware(ctx), FakeMessage("/users", OWNER))
    assert actor is not None and actor.is_owner


async def test_a_stranger_gets_silence_for_everything_but_start(
    ctx: BotContext,
) -> None:
    message = FakeMessage("/stats", STRANGER)
    assert await run(AuthMiddleware(ctx), message) is None
    assert message.replies == [], "silence is literal — no reply, not a refusal"


async def test_a_stranger_reaches_the_start_handler(ctx: BotContext) -> None:
    """Only the handler may create a row; the gate just gets out of the way."""
    actor = await run(AuthMiddleware(ctx), FakeMessage("/start", STRANGER))
    assert actor is not None
    assert actor.account is None
    assert actor.user_id == STRANGER


async def test_a_button_press_from_a_stranger_is_dropped_without_a_toast(
    ctx: BotContext,
) -> None:
    query = FakeQuery("sig:whatever", STRANGER)
    assert await run(AuthMiddleware(ctx), query) is None
    assert query.answers == [], "answering the query would confirm the bot is live"


async def test_a_pending_user_is_told_where_they_stand(ctx: BotContext, store: FakeStore) -> None:
    store.users[MEMBER] = member_account(MEMBER, status=UserStatus.PENDING, acknowledged=False)
    message = FakeMessage("/start", MEMBER)
    assert await run(AuthMiddleware(ctx), message) is None
    assert "waiting" in message.replies[0]


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (UserStatus.REJECTED, "declined"),
        (UserStatus.SUSPENDED, "suspended"),
        (UserStatus.LEFT, "You left"),
    ],
)
async def test_each_refused_standing_says_what_it_is(
    ctx: BotContext, store: FakeStore, status: UserStatus, phrase: str
) -> None:
    """Same silence, different sentences. A rejected stranger and a member who
    resigned are completely different facts and neither should be a mystery."""
    store.users[MEMBER] = member_account(MEMBER, status=status, acknowledged=False)
    message = FakeMessage("/start", MEMBER)
    await run(AuthMiddleware(ctx), message)
    assert phrase in message.replies[0]


async def test_a_rejected_user_cannot_make_the_bot_hold_a_conversation(
    ctx: BotContext, store: FakeStore, clock: FrozenClock
) -> None:
    """One notice per hour per id, and the throttle is a stored timestamp.

    Without it, a rejected stranger tapping /start has a bot that answers every
    time — which is both a nuisance and a way to keep the process busy.
    """
    store.users[MEMBER] = member_account(MEMBER, status=UserStatus.REJECTED, acknowledged=False)
    middleware = AuthMiddleware(ctx)

    first = FakeMessage("/start", MEMBER)
    await run(middleware, first)
    assert len(first.replies) == 1
    assert store.users[MEMBER].notice_at == clock.now()

    second = FakeMessage("/start", MEMBER)
    await run(middleware, second)
    assert second.replies == [], "the second /start within the hour is silent"


async def test_the_throttle_lapses(ctx: BotContext, store: FakeStore, clock: FrozenClock) -> None:
    store.users[MEMBER] = member_account(
        MEMBER, status=UserStatus.REJECTED, acknowledged=False
    ).model_copy(update={"notice_at": clock.now() - timedelta(hours=2)})
    message = FakeMessage("/start", MEMBER)
    await run(AuthMiddleware(ctx), message)
    assert len(message.replies) == 1


async def test_an_unacknowledged_user_is_asked_again_rather_than_ignored(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, acknowledged=False)
    message = FakeMessage("/stats", MEMBER)
    assert await run(AuthMiddleware(ctx), message) is None
    assert "experimental" in message.replies[0]
    assert message.markups[0] is not None, "the note must arrive with its button"


async def test_an_unacknowledged_user_pressing_a_button_gets_an_alert(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, acknowledged=False)
    query = FakeQuery("sig:whatever", MEMBER)
    assert await run(AuthMiddleware(ctx), query) is None
    assert query.answers and query.answers[0][1] is True


async def test_an_update_with_no_sender_is_dropped(ctx: BotContext) -> None:
    """A channel post or an edited message with no ``from`` — fail closed."""
    ran = False

    async def handler(_: Any, __: dict[str, Any]) -> None:
        nonlocal ran
        ran = True

    await AuthMiddleware(ctx)(handler, FakeMessage(), {})  # type: ignore[arg-type]
    assert ran is False


# --------------------------------------------------------------------------- #
# OwnerOnly
# --------------------------------------------------------------------------- #


async def test_owner_only_admits_the_owner_and_nobody_else() -> None:
    """A filter that does not match means no handler runs, which *is* silence — so a
    member's /approve reveals nothing, not even that the command exists."""
    owner = Actor(user_id=OWNER, account=owner_account(OWNER))
    member = Actor(user_id=MEMBER, account=member_account(MEMBER))
    stranger = Actor(user_id=STRANGER)

    assert await OwnerOnly()(FakeMessage(), owner) is True  # type: ignore[arg-type]
    assert await OwnerOnly()(FakeMessage(), member) is False  # type: ignore[arg-type]
    assert await OwnerOnly()(FakeMessage(), stranger) is False  # type: ignore[arg-type]


def test_an_unknown_actor_cannot_be_mistaken_for_a_known_one() -> None:
    with pytest.raises(AssertionError):
        Actor(user_id=STRANGER).known()


def test_the_fake_accounts_match_the_real_defaults() -> None:
    """Guard on the test helper itself: ``owner_account`` must produce something the
    production code would call active, or every test above proves nothing."""
    assert owner_account().eligible_for_signals(ACCOUNT_NOW)
    assert not owner_account(acknowledged=False).eligible_for_signals(ACCOUNT_NOW)
