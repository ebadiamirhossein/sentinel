"""Member watchlist requests — specs/TELEGRAM_UX.md §3a (M8.3).

The four properties this feature promises, asserted rather than described:

* **members ask, they never edit** — the watchlist is shared and the owner pays;
* **one pending request per symbol**, and a duplicate is a no-op that reaches nobody;
* **the cap binds everybody**, including the owner, and is re-checked at the moment
  of approval rather than only when the request was made;
* **a request that cannot be granted because the list filled up stays PENDING**, and
  both people are told — it is not a rejection, because nobody decided anything.

Reachability through the real dispatcher lives in ``test_dispatcher_wiring.py``;
this file is about what the handlers do once they are reached.
"""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.bot.auth import Actor
from sentinel.bot.context import BotContext
from sentinel.bot.handlers import admin, commands
from sentinel.bot.keyboards import AdminAction, WatchlistCallback
from sentinel.bot.models import WatchlistRequestStatus
from sentinel.bot.runtime import WATCHLIST
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from tests.bot_double import (
    FakeBot,
    FakeDatabase,
    FakeStore,
    fake_repositories,
    member_account,
    owner_account,
)

OWNER = 111
MEMBER = 222
OTHER = 333

#: Not on the default watchlist, so a request for it is a real request.
NEW = "ATOMUSDT"


class FakeMessage:
    def __init__(self, user_id: int = MEMBER) -> None:
        self.from_user = type("U", (), {"id": user_id})()
        self.replies: list[str] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.replies.append(text)

    @property
    def last(self) -> str:
        return self.replies[-1]


class FakeQuery:
    def __init__(self, user_id: int = OWNER) -> None:
        self.from_user = type("U", (), {"id": user_id})()
        self.message = FakeMessage(user_id)
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        pass


class FakeCommand:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, username="owner")
    store.users[MEMBER] = member_account(MEMBER, username="friend")
    store.users[OTHER] = member_account(OTHER, username="other")
    return store


@pytest.fixture
def ctx(store: FakeStore, tz: Any, clock: FrozenClock) -> BotContext:
    return BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=load_config()),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=clock,
        tz=tz,
        repositories=fake_repositories(),
    )


def member_actor(store: FakeStore, user_id: int = MEMBER) -> Actor:
    return Actor(user_id=user_id, username="friend", account=store.users[user_id])


def owner_actor(store: FakeStore) -> Actor:
    return Actor(user_id=OWNER, username="owner", account=store.users[OWNER])


def watchlist(ctx: BotContext, store: FakeStore) -> tuple[str, ...]:
    stored = store.settings.get(WATCHLIST)
    return tuple(stored) if stored is not None else tuple(ctx.settings.config.watchlist)


async def ask(
    ctx: BotContext, store: FakeStore, symbol: str, *, user_id: int = MEMBER
) -> tuple[FakeMessage, FakeBot]:
    message, bot = FakeMessage(user_id), FakeBot()
    await commands.request(message, FakeCommand(symbol), ctx, member_actor(store, user_id), bot)
    return message, bot


async def decide(
    ctx: BotContext, store: FakeStore, symbol: str, *, approve: bool
) -> tuple[FakeQuery, FakeBot]:
    query, bot = FakeQuery(OWNER), FakeBot()
    action = AdminAction.APPROVE if approve else AdminAction.REJECT
    await admin.watchlist_request_button(
        query, WatchlistCallback(symbol=symbol, action=action), ctx, owner_actor(store), bot
    )
    return query, bot


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #


async def test_a_request_is_stored_and_reaches_the_owner(ctx: BotContext, store: FakeStore) -> None:
    message, bot = await ask(ctx, store, NEW)

    stored = store.watchlist_requests[NEW]
    assert stored.symbol == NEW
    assert stored.requested_by_user_id == MEMBER
    assert stored.status is WatchlistRequestStatus.PENDING
    assert NEW in message.last

    sent = bot.of("send_message")
    assert len(sent) == 1
    assert sent[0].kwargs["chat_id"] == OWNER
    assert NEW in sent[0].kwargs["text"]
    assert "@friend" in sent[0].kwargs["text"], "the owner needs to know who asked"
    assert sent[0].kwargs["reply_markup"] is not None, "and needs the two buttons"


async def test_asking_does_not_touch_the_watchlist(ctx: BotContext, store: FakeStore) -> None:
    """The whole point: a member cannot spend the owner's budget by asking."""
    before = watchlist(ctx, store)
    await ask(ctx, store, NEW)
    assert watchlist(ctx, store) == before
    assert store.changes == [], "no config change until the owner approves"


async def test_a_duplicate_request_is_a_no_op_and_reaches_nobody(
    ctx: BotContext, store: FakeStore
) -> None:
    """One pending per symbol — the database's guarantee, not a pre-check.

    Asserted from the *second* asker's side too: a member learns that the symbol is
    spoken for, and nothing tells them who asked. Who else uses this bot is not their
    business (M8.1 §6's boundary).
    """
    await ask(ctx, store, NEW)
    message, bot = await ask(ctx, store, NEW, user_id=OTHER)

    assert len(store.watchlist_requests) == 1
    assert bot.of("send_message") == [], "the owner is not asked twice"
    assert "already been requested" in message.last
    assert "friend" not in message.last, "the first asker's identity is not disclosed"


async def test_a_symbol_already_watched_is_refused_with_its_own_message(
    ctx: BotContext, store: FakeStore
) -> None:
    message, bot = await ask(ctx, store, "SOLUSDT")
    assert "already on the watchlist" in message.last
    assert store.watchlist_requests == {}
    assert bot.of("send_message") == []


async def test_a_nonsense_symbol_never_reaches_the_owner(ctx: BotContext, store: FakeStore) -> None:
    """Validated at the door, so a typo is answered by the person who made it."""
    message, bot = await ask(ctx, store, "not a symbol!!")
    assert "does not look like a symbol" in message.last
    assert store.watchlist_requests == {}
    assert bot.of("send_message") == []


async def test_request_without_an_argument_says_how(ctx: BotContext, store: FakeStore) -> None:
    message, bot = FakeMessage(MEMBER), FakeBot()
    await commands.request(message, FakeCommand(None), ctx, member_actor(store), bot)
    assert "/request" in message.last
    assert store.watchlist_requests == {}


async def test_the_owner_is_pointed_at_watchlist_add(ctx: BotContext, store: FakeStore) -> None:
    """They have the direct command; a request path would have them approve themselves."""
    message, bot = FakeMessage(OWNER), FakeBot()
    await commands.request(message, FakeCommand(NEW), ctx, owner_actor(store), bot)
    assert "/watchlist add" in message.last
    assert store.watchlist_requests == {}


# --------------------------------------------------------------------------- #
# The cap
# --------------------------------------------------------------------------- #


def fill_to_cap(ctx: BotContext, store: FakeStore) -> None:
    cap = ctx.settings.config.watchlist_max_symbols
    store.settings[WATCHLIST] = [f"FILL{index:02d}USDT" for index in range(cap)]


async def test_a_request_past_the_cap_is_refused_at_the_door(
    ctx: BotContext, store: FakeStore
) -> None:
    fill_to_cap(ctx, store)
    message, bot = await ask(ctx, store, NEW)

    assert "full" in message.last
    assert str(ctx.settings.config.watchlist_max_symbols) in message.last
    assert store.watchlist_requests == {}
    assert bot.of("send_message") == [], "an ungrantable request does not reach the owner"


async def test_the_cap_binds_the_owners_own_watchlist_add(
    ctx: BotContext, store: FakeStore
) -> None:
    """Owner ruling 2026-08-19: a cap that applies only to other people is not a cap."""
    fill_to_cap(ctx, store)
    before = watchlist(ctx, store)
    message = FakeMessage(OWNER)
    await admin.watchlist(message, FakeCommand(f"add {NEW}"), ctx, owner_actor(store))

    assert "full" in message.last
    assert watchlist(ctx, store) == before


async def test_the_cap_is_rechecked_at_approval_and_the_request_survives(
    ctx: BotContext, store: FakeStore
) -> None:
    """The case only the moment of approval can know about.

    A request legal when it was made, against a watchlist that filled up while it
    waited. Both people are told, and the row stays PENDING — nobody decided against
    the symbol, so the owner can make room and approve the same card rather than the
    member having to ask again.
    """
    await ask(ctx, store, NEW)
    fill_to_cap(ctx, store)
    query, bot = await decide(ctx, store, NEW, approve=True)

    assert store.watchlist_requests[NEW].status is WatchlistRequestStatus.PENDING
    assert NEW not in watchlist(ctx, store)
    assert store.changes == [], "nothing was written"

    told = {call.kwargs["chat_id"]: call.kwargs["text"] for call in bot.of("send_message")}
    assert set(told) == {OWNER, MEMBER}, "both sides hear about it"
    assert "not been declined" in told[MEMBER]
    assert "still <b>pending</b>" in told[OWNER]
    assert "full" in query.answers[0].lower()


async def test_making_room_then_approving_works(ctx: BotContext, store: FakeStore) -> None:
    """The point of keeping it PENDING: the same card works once there is space."""
    await ask(ctx, store, NEW)
    fill_to_cap(ctx, store)
    await decide(ctx, store, NEW, approve=True)

    store.settings[WATCHLIST] = store.settings[WATCHLIST][:-1]
    await decide(ctx, store, NEW, approve=True)

    assert NEW in watchlist(ctx, store)
    assert store.watchlist_requests == {}


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #


async def test_approval_adds_the_symbol_and_audits_it_like_a_manual_edit(
    ctx: BotContext, store: FakeStore
) -> None:
    """The same write ``/watchlist add`` makes, so PRD F10's audit does not fork."""
    await ask(ctx, store, NEW)
    _, bot = await decide(ctx, store, NEW, approve=True)

    assert NEW in watchlist(ctx, store)
    assert store.changes, "an approval must leave a config_changes row"
    key, _, new_value, by = store.changes[-1]
    assert key == WATCHLIST
    assert NEW in new_value
    assert by == OWNER, "the owner made the change, not the member who asked"

    told = bot.of("send_message")
    assert [call.kwargs["chat_id"] for call in told] == [MEMBER]
    assert "added to the watchlist" in told[0].kwargs["text"]


async def test_rejection_tells_the_member_and_changes_nothing(
    ctx: BotContext, store: FakeStore
) -> None:
    """No reason required — but silence would be worse than a bare no."""
    await ask(ctx, store, NEW)
    before = watchlist(ctx, store)
    _, bot = await decide(ctx, store, NEW, approve=False)

    assert watchlist(ctx, store) == before
    assert store.changes == []
    told = bot.of("send_message")
    assert [call.kwargs["chat_id"] for call in told] == [MEMBER]
    assert "not added" in told[0].kwargs["text"].lower()


async def test_a_rejected_symbol_can_be_asked_for_again(ctx: BotContext, store: FakeStore) -> None:
    """Owner ruling: no cooldown. The reason to hold a symbol is a fact about the
    market, and markets change; ``/suspend`` exists for somebody who abuses it."""
    await ask(ctx, store, NEW)
    await decide(ctx, store, NEW, approve=False)

    message, bot = await ask(ctx, store, NEW)
    assert NEW in store.watchlist_requests
    assert bot.of("send_message"), "the owner is asked again"
    assert "already been requested" not in message.last


async def test_a_second_tap_on_an_answered_card_changes_nothing(
    ctx: BotContext, store: FakeStore
) -> None:
    """Scoped to the PENDING row, so a stale card cannot re-decide anything."""
    await ask(ctx, store, NEW)
    await decide(ctx, store, NEW, approve=True)
    changes_before = len(store.changes)

    query, bot = await decide(ctx, store, NEW, approve=False)

    assert NEW in watchlist(ctx, store), "the approval stands"
    assert len(store.changes) == changes_before
    assert bot.of("send_message") == []
    assert "already been answered" in query.answers[0]


async def test_the_decided_row_records_who_and_when(ctx: BotContext, store: FakeStore) -> None:
    """PRD G5 — a decision that cannot be attributed is not an audit trail."""
    await ask(ctx, store, NEW)
    await decide(ctx, store, NEW, approve=True)

    decided = store.watchlist_history[-1]
    assert decided.symbol == NEW
    assert decided.status is WatchlistRequestStatus.APPROVED
    assert decided.decided_by_user_id == OWNER
    assert decided.requested_by_user_id == MEMBER
    assert decided.decided_at is not None
