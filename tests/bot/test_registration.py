"""Joining, approval, acknowledgement and leaving — specs/TELEGRAM_UX.md §7 (M8.1).

The four properties this milestone actually promises, each asserted rather than
described:

* **one request per id**, guaranteed by a primary key rather than a counter;
* **nothing is delivered before the note is acknowledged**, and what was accepted
  is identifiable;
* **leaving needs nobody's permission**, and takes effect immediately;
* **``/users`` shows enough to operate the system and nothing more** — the owner
  ruling that this milestone must not quietly widen later.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from sentinel.bot.auth import Actor
from sentinel.bot.context import BotContext
from sentinel.bot.handlers import admin, membership
from sentinel.bot.keyboards import AckCallback, AdminAction, AdminCallback, LeaveCallback
from sentinel.bot.models import ACK_VERSION, UserRole, UserStatus
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
STRANGER = 333


class FakeMessage:
    def __init__(self, user_id: int = OWNER) -> None:
        self.from_user = type("U", (), {"id": user_id})()
        self.replies: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.replies.append(text)
        self.markups.append(reply_markup)

    @property
    def last(self) -> str:
        return self.replies[-1]


class FakeQuery:
    """A callback with a message under it, so the handler's edits are observable."""

    def __init__(self, user_id: int = OWNER) -> None:
        self.from_user = type("U", (), {"id": user_id})()
        self.message = FakeMessage(user_id)
        self.answers: list[str] = []
        self.markup_edits: list[Any] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:  # pragma: no cover
        self.markup_edits.append(reply_markup)


class FakeCommand:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, username="owner")
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


def owner_actor(store: FakeStore) -> Actor:
    return Actor(user_id=OWNER, account=store.users[OWNER])


def stranger_actor() -> Actor:
    return Actor(user_id=STRANGER, username="newcomer", display_name="New Comer")


# --------------------------------------------------------------------------- #
# /start — the one crack in the door
# --------------------------------------------------------------------------- #


async def test_a_stranger_start_creates_one_request_and_tells_the_owner(
    ctx: BotContext, store: FakeStore
) -> None:
    bot = FakeBot()
    message = FakeMessage(STRANGER)
    await membership.start(message, ctx, stranger_actor(), bot)

    created = store.users[STRANGER]
    assert created.status is UserStatus.PENDING
    assert created.role is UserRole.MEMBER
    assert created.username == "newcomer"
    assert "waiting" in message.last

    sent = bot.of("send_message")
    assert len(sent) == 1
    assert sent[0].kwargs["chat_id"] == OWNER
    assert "@newcomer" in sent[0].kwargs["text"]
    assert str(STRANGER) in sent[0].kwargs["text"]
    assert sent[0].kwargs["reply_markup"] is not None, "Approve/Reject must be attached"


async def test_a_second_start_is_a_no_op_and_reaches_nobody(
    ctx: BotContext, store: FakeStore
) -> None:
    """One request per id, and the primary key is what guarantees it.

    A counter would have to survive restarts and races; ``ON CONFLICT DO NOTHING``
    already does. Note this handler is only reached at all on a *first* ``/start``
    — the gate answers a known PENDING id itself, throttled.
    """
    bot = FakeBot()
    await membership.start(FakeMessage(STRANGER), ctx, stranger_actor(), bot)
    requested_at = store.users[STRANGER].requested_at

    await membership.start(FakeMessage(STRANGER), ctx, stranger_actor(), bot)

    assert len(bot.of("send_message")) == 1, "the owner is told once, ever"
    assert store.users[STRANGER].requested_at == requested_at, "the row is untouched"


async def test_a_request_is_stored_even_when_there_is_no_owner_to_tell(
    ctx: BotContext, store: FakeStore
) -> None:
    """A misconfigured deployment must not lose somebody's request in silence."""
    del store.users[OWNER]
    bot = FakeBot()
    await membership.start(FakeMessage(STRANGER), ctx, stranger_actor(), bot)

    assert store.users[STRANGER].status is UserStatus.PENDING
    assert bot.of("send_message") == []


async def test_start_from_a_set_up_user_summarises_rather_than_re_registering(
    ctx: BotContext, store: FakeStore
) -> None:
    bot = FakeBot()
    message = FakeMessage(OWNER)
    await membership.start(message, ctx, owner_actor(store), bot)
    assert "capital" in message.last.lower()
    assert bot.calls == []


# --------------------------------------------------------------------------- #
# Approve / reject / suspend
# --------------------------------------------------------------------------- #


async def test_approving_greets_the_member_and_asks_for_the_acknowledgement(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, status=UserStatus.PENDING, acknowledged=False)
    bot = FakeBot()
    message = FakeMessage(OWNER)

    await admin.approve(message, FakeCommand(str(MEMBER)), ctx, owner_actor(store), bot)

    assert store.users[MEMBER].status is UserStatus.APPROVED
    assert store.users[MEMBER].decided_by_user_id == OWNER
    texts = [call.kwargs["text"] for call in bot.of("send_message")]
    assert any("You're in" in text for text in texts)
    assert any("experimental" in text for text in texts)
    assert MEMBER in [int(key) for key in bot.scopes()], "their menu is published on approval"


async def test_rejecting_tells_them_once_and_clears_their_menu(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, status=UserStatus.PENDING, acknowledged=False)
    bot = FakeBot()

    await admin.reject(FakeMessage(OWNER), FakeCommand(str(MEMBER)), ctx, owner_actor(store), bot)

    assert store.users[MEMBER].status is UserStatus.REJECTED
    assert "declined" in bot.of("send_message")[0].kwargs["text"]
    assert bot.of("delete_my_commands"), "the menu goes with the access"


async def test_suspending_stops_delivery_without_deleting_anything(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, capital_eur=Decimal("5000"))
    bot = FakeBot()
    message = FakeMessage(OWNER)

    await admin.suspend(message, FakeCommand(str(MEMBER)), ctx, owner_actor(store), bot)

    assert store.users[MEMBER].status is UserStatus.SUSPENDED
    assert store.users[MEMBER].capital_eur == Decimal("5000"), "their settings survive"
    assert "history is untouched" in message.last


async def test_the_owner_cannot_suspend_themselves(ctx: BotContext, store: FakeStore) -> None:
    """It would leave the system with users and nobody able to operate it."""
    bot = FakeBot()
    message = FakeMessage(OWNER)
    await admin.suspend(message, FakeCommand(str(OWNER)), ctx, owner_actor(store), bot)

    assert store.users[OWNER].status is UserStatus.APPROVED
    assert "That is you" in message.last


async def test_an_unknown_id_says_so_instead_of_appearing_to_work(
    ctx: BotContext, store: FakeStore
) -> None:
    bot = FakeBot()
    message = FakeMessage(OWNER)
    await admin.approve(message, FakeCommand("999999"), ctx, owner_actor(store), bot)
    assert "No user with id" in message.last


@pytest.mark.parametrize("args", [None, "not-a-number", ""])
async def test_a_malformed_id_gets_the_usage_line(
    ctx: BotContext, store: FakeStore, args: str | None
) -> None:
    bot = FakeBot()
    message = FakeMessage(OWNER)
    await admin.approve(message, FakeCommand(args), ctx, owner_actor(store), bot)
    assert "Usage" in message.last


async def test_the_approve_button_does_the_same_thing_as_the_command(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, status=UserStatus.PENDING, acknowledged=False)
    bot = FakeBot()
    query = FakeQuery(OWNER)

    await admin.approval_button(
        query,
        AdminCallback(user_id=MEMBER, action=AdminAction.APPROVE),
        ctx,
        owner_actor(store),
        bot,
    )

    assert store.users[MEMBER].status is UserStatus.APPROVED
    assert query.answers == ["Approved."]


# --------------------------------------------------------------------------- #
# The first-run acknowledgement
# --------------------------------------------------------------------------- #


async def test_acknowledging_records_the_wording_that_was_accepted(
    ctx: BotContext, store: FakeStore, clock: FrozenClock
) -> None:
    store.users[MEMBER] = member_account(MEMBER, acknowledged=False)
    query = FakeQuery(MEMBER)

    await membership.acknowledge(
        query, AckCallback(version=ACK_VERSION), ctx, Actor(MEMBER, account=store.users[MEMBER])
    )

    account = store.users[MEMBER]
    assert account.acknowledged_at == clock.now()
    assert account.acknowledged_version == ACK_VERSION
    assert account.acknowledged is True


async def test_a_button_from_a_superseded_note_is_refused(
    ctx: BotContext, store: FakeStore
) -> None:
    """Recording it would log consent to wording this person never saw."""
    store.users[MEMBER] = member_account(MEMBER, acknowledged=False)
    query = FakeQuery(MEMBER)

    await membership.acknowledge(
        query, AckCallback(version="v0"), ctx, Actor(MEMBER, account=store.users[MEMBER])
    )

    assert store.users[MEMBER].acknowledged_at is None
    assert "updated" in query.answers[0]
    assert "experimental" in query.message.last, "the current note is sent instead"


async def test_acknowledging_without_capital_says_what_is_still_missing(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, acknowledged=False)
    query = FakeQuery(MEMBER)
    await membership.acknowledge(
        query, AckCallback(version=ACK_VERSION), ctx, Actor(MEMBER, account=store.users[MEMBER])
    )
    assert "/capital" in query.message.last


# --------------------------------------------------------------------------- #
# /leave
# --------------------------------------------------------------------------- #


async def test_leaving_takes_one_confirmation_and_then_takes_effect(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER, capital_eur=Decimal("5000"))
    actor = Actor(MEMBER, account=store.users[MEMBER])
    bot = FakeBot()

    prompt = FakeMessage(MEMBER)
    await membership.leave(prompt, ctx, actor)
    assert prompt.markups[-1] is not None, "leaving is one tap away, not zero"
    assert store.users[MEMBER].status is UserStatus.APPROVED

    query = FakeQuery(MEMBER)
    await membership.leave_confirmation(query, LeaveCallback(confirm=True), ctx, actor, bot)

    assert store.users[MEMBER].status is UserStatus.LEFT
    assert store.users[MEMBER].capital_eur == Decimal("5000"), "history is kept, not deleted"
    assert bot.of("delete_my_commands"), "their menu goes with them"
    assert "removed" in query.message.last.lower()


async def test_declining_the_leave_confirmation_changes_nothing(
    ctx: BotContext, store: FakeStore
) -> None:
    store.users[MEMBER] = member_account(MEMBER)
    query = FakeQuery(MEMBER)
    await membership.leave_confirmation(
        query,
        LeaveCallback(confirm=False),
        ctx,
        Actor(MEMBER, account=store.users[MEMBER]),
        FakeBot(),
    )
    assert store.users[MEMBER].status is UserStatus.APPROVED


async def test_the_owner_cannot_leave(ctx: BotContext, store: FakeStore) -> None:
    """There would be nobody left able to approve, suspend or operate anything."""
    message = FakeMessage(OWNER)
    await membership.leave(message, ctx, owner_actor(store))
    assert "You are the owner" in message.last
    assert message.markups[-1] is None, "no confirmation button to mis-tap"
    assert store.users[OWNER].status is UserStatus.APPROVED


# --------------------------------------------------------------------------- #
# /users — and the privacy boundary
# --------------------------------------------------------------------------- #


async def test_users_lists_standing_and_setup_state(ctx: BotContext, store: FakeStore) -> None:
    store.users[MEMBER] = member_account(MEMBER, username="friend", capital_eur=Decimal("5000"))
    store.users[STRANGER] = member_account(STRANGER, status=UserStatus.PENDING, acknowledged=False)
    message = FakeMessage(OWNER)

    await admin.users(message, ctx)

    card = message.last
    assert "@friend" in card
    assert "APPROVED" in card and "PENDING" in card
    assert str(MEMBER) in card
    assert "no capital set" in card, "who is stuck is operational information"


async def test_users_never_shows_an_amount_a_pnl_or_a_decision(
    ctx: BotContext, store: FakeStore
) -> None:
    """The owner ruling this milestone must not quietly widen later.

    Running a system for friends requires knowing who is set up and who is stuck. It
    does not require watching them trade — so the amount is absent, and the view
    object has no field that could carry it back in.
    """
    from sentinel.bot.views import UserView

    store.users[MEMBER] = member_account(
        MEMBER, username="friend", capital_eur=Decimal("5000"), risk_per_trade_pct=Decimal("1.25")
    )
    message = FakeMessage(OWNER)
    await admin.users(message, ctx)

    assert "5000" not in message.last, "a member's capital is not the owner's business"
    assert "1.25" not in message.last, "nor is their risk setting"
    assert "not shown here" in message.last, "and the card says so, so nobody wonders"

    fields = set(UserView.__dataclass_fields__)
    forbidden = {"capital_eur", "risk_per_trade_pct", "realized_r", "win_rate", "decision"}
    assert fields & forbidden == set(), (
        "UserView must carry no field a future card could print — the boundary is "
        "a type, not a renderer's discretion"
    )
