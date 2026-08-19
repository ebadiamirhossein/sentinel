"""Router wiring, driven through a **real** ``aiogram.Dispatcher`` (M8.2 §1).

Every other test in ``tests/bot/`` calls a handler, or the gate, directly through
``tests/bot_double.py``. That is the right shape for behaviour — a real
``aiogram.Message`` cannot be built without a bound ``Bot`` to answer through — but
it has one blind spot, and the blind spot shipped:

**aiogram resolves a sub-router's root filters before that router's handlers, and
therefore before any *inner* middleware runs.** ``admin_router`` carries
``OwnerOnly()`` as a root filter; ``OwnerOnly`` takes ``actor``; ``AuthMiddleware``
is what injects ``actor``. Registered as inner middleware, the filter was called
with ``actor`` missing, aiogram raised ``TypeError``, its error middleware swallowed
it, and the update was logged "not handled". Every owner command answered with
silence — and silence is exactly what ``OwnerOnly`` is *supposed* to produce for a
non-owner, so a completely dead admin surface was indistinguishable from working
access control. It reached production and was found by a human typing ``/pause``.

Calling handlers directly cannot catch that, because the bug is not in a handler. So
these tests feed real ``Update`` objects to a real ``Dispatcher`` and assert on
propagation. It is the same lesson as ``check-image`` (journal/M6_REPORT.md §12) one
layer in: the thing that only happens in the real harness must be exercised in the
real harness.

The centre of the file is :func:`test_every_owner_command_is_reachable_by_the_owner`
and its meta-test. M8.1 shipped 37 auth tests and a 28-cell truth table, all of which
asserted that non-owners are **refused** — a property that passes trivially when
*everybody* is refused. Silence as a failure mode needs a positive test.

Two mechanics worth knowing before editing this file:

* **The routers are module-level singletons.** ``aiogram`` refuses to attach one to a
  second ``Dispatcher``, so the dispatcher here is module-scoped and the fake store
  is reset between tests rather than rebuilt.
* **``feed_update`` returns the handler's return value**, and these handlers return
  ``None``. "Nothing handled this" is the ``UNHANDLED`` sentinel, not ``None`` —
  asserting against ``None`` would pass on a dead router and prove nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from sentinel.bot.app import build_dispatcher
from sentinel.bot.auth import AuthMiddleware
from sentinel.bot.context import BotContext
from sentinel.bot.keyboards import AdminAction, AdminCallback, WatchlistCallback
from sentinel.bot.menu import MEMBER_COMMANDS, OWNER_COMMANDS
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

#: Arguments that make each owner command's *handler* run rather than bail on a
#: usage error. The point under test is reachability — whether the update gets past
#: the router at all — so anything that parses is enough.
OWNER_COMMAND_ARGS: dict[str, str] = {
    "status": "",
    "settings": "",
    "watchlist": "",
    "pause": "",
    "resume": "",
    "users": "",
    "approve": f" {MEMBER}",
    "reject": f" {MEMBER}",
    "suspend": f" {MEMBER}",
}


#: The member half. ``/leave`` opens a confirmation rather than acting, which is
#: still a handler running — reachability is the question here, not effect.
MEMBER_COMMAND_ARGS: dict[str, str] = {
    "help": "",
    "capital": " 5000",
    "risk": " 0.75",
    "positions": "",
    "stats": "",
    "request": " SOLUSDT",
    "leave": "",
}


class FakeBot:
    """Enough of a ``Bot`` for ``feed_update``; every outbound call is swallowed.

    Deliberately permissive. These tests ask one question — *did the update reach a
    handler* — and a handler that is reached goes on to answer, publish a menu or
    edit a card. Modelling each of those precisely is ``tests/bot_double.py``'s job
    and is already done there; re-doing it here would make this file about outbound
    behaviour instead of about routing, and a missing method would fail as "not
    reachable" when the truth is the opposite.

    ``calls`` records the method names anyway, so a test that wants to assert on a
    side effect can.
    """

    id = 8858349395

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        """aiogram's own call path: handlers build a method object and await the bot."""
        self.calls.append(type(method).__name__)
        return None

    def __getattr__(self, name: str) -> Any:
        """Named Bot helpers (``set_my_commands``, ``delete_my_commands``, …)."""

        async def _noop(*args: Any, **kwargs: Any) -> None:
            self.calls.append(name)

        return _noop


@pytest.fixture(scope="module")
def store() -> FakeStore:
    """Module-scoped, because the dispatcher that reads it has to be."""
    return FakeStore()


@pytest.fixture(scope="module")
def dispatcher(store: FakeStore) -> Dispatcher:
    """One dispatcher for the module — the routers cannot be attached twice."""
    ctx = BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=load_config()),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(ACCOUNT_NOW),
        tz=ZoneInfo("Europe/Berlin"),
        repositories=fake_repositories(),
    )
    return build_dispatcher(ctx)


@pytest.fixture(autouse=True)
def _fresh_rows(store: FakeStore) -> None:
    """Reset the rows each test reads, since the store outlives the test."""
    store.users.clear()
    store.users[OWNER] = owner_account(OWNER)
    store.users[MEMBER] = member_account(MEMBER)
    store.pause = type(store.pause)()


def message_update(text: str, *, user_id: int, update_id: int = 1) -> Update:
    user = TgUser(id=user_id, is_bot=False, first_name="Some", last_name="One")
    chat = Chat(id=user_id, type="private")
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
        ),
    )


async def feed(dispatcher: Dispatcher, update: Update) -> Any:
    return await dispatcher.feed_update(FakeBot(), update)  # type: ignore[arg-type]


def reached_a_handler(response: Any) -> bool:
    """Whether propagation found a handler.

    ``UNHANDLED`` is aiogram's sentinel for "no handler matched"; a handler that ran
    and returned ``None`` is a *different* outcome, and the distinction is the whole
    point here — the defect under test made every owner command return neither an
    error nor a reply.
    """
    return response is not UNHANDLED


# --------------------------------------------------------------------------- #
# The mechanism
# --------------------------------------------------------------------------- #


def test_the_gate_is_an_outer_middleware_on_both_observers(dispatcher: Dispatcher) -> None:
    """The invariant, named directly rather than only through its symptom.

    Inner middleware wraps a handler once one has matched. A sub-router's root filters
    are resolved *before* that, so a filter needing ``actor`` — ``OwnerOnly`` — sees
    nothing. Stating it structurally means a future refactor that moves the gate back
    to ``middleware()`` fails here, with the reason, rather than in production as
    silence.
    """
    for observer in (dispatcher.message, dispatcher.callback_query):
        outer = observer.outer_middleware._middlewares
        inner = observer.middleware._middlewares
        assert any(isinstance(m, AuthMiddleware) for m in outer), (
            "the auth gate must be OUTER: root filters on sub-routers run before "
            "inner middleware, so `actor` would not be injected in time"
        )
        assert not any(isinstance(m, AuthMiddleware) for m in inner)


# --------------------------------------------------------------------------- #
# The regression itself
# --------------------------------------------------------------------------- #


async def test_pause_from_the_owner_reaches_a_handler(dispatcher: Dispatcher) -> None:
    """The exact update that produced silence in production on 2026-08-19.

    Fails on the inner-middleware wiring with
    ``TypeError: OwnerOnly.__call__() missing 1 required positional argument: 'actor'``.
    """
    response = await feed(dispatcher, message_update("/pause", user_id=OWNER))
    assert reached_a_handler(response), "/pause from the owner reached no handler"


async def test_pause_actually_pauses(dispatcher: Dispatcher, store: FakeStore) -> None:
    """Reachability is necessary but not sufficient — the side effect is the point.

    ``/pause`` is the owner's stop button for a system that spends money unattended;
    "the update was routed" is not the same promise as "new signals stopped".
    """
    await feed(dispatcher, message_update("/pause", user_id=OWNER))
    assert store.pause.paused is True


@pytest.mark.parametrize("command", sorted(OWNER_COMMAND_ARGS))
async def test_every_owner_command_is_reachable_by_the_owner(
    dispatcher: Dispatcher, command: str
) -> None:
    """Positive reachability for all nine, not only "non-owners are refused".

    The property M8.1 asserted — a member gets silence — is true of a working system
    and equally true of a dead one. This is the half that tells them apart.
    """
    text = f"/{command}{OWNER_COMMAND_ARGS[command]}"
    response = await feed(dispatcher, message_update(text, user_id=OWNER))
    assert reached_a_handler(response), f"/{command} from the owner reached no handler"


def test_the_reachability_list_covers_every_advertised_owner_command() -> None:
    """Meta-test: a tenth owner command cannot be added without a reachability test.

    ``OWNER_COMMANDS`` is what ``setMyCommands`` publishes into the owner's ``/`` menu,
    so this is "everything we told the owner exists" against "everything we proved
    reaches a handler". Without it, adding a command would leave a silent untested one
    — the same hole M7 §3's transition-table meta-test closes for the state machine,
    and for the same reason: nothing else tells you what you forgot.
    """
    advertised = {command.command for command in OWNER_COMMANDS}
    assert set(OWNER_COMMAND_ARGS) == advertised


async def test_the_admin_buttons_are_reachable_by_the_owner(dispatcher: Dispatcher) -> None:
    """``AdminCallback`` sits behind the same root filter and was dead in the same way.

    The Approve/Reject buttons under a registration request are how M8.1 expects an
    owner to admit somebody — the milestone's headline feature. They are a
    ``callback_query``, i.e. the other observer, so they need their own assertion.
    """
    update = Update(
        update_id=2,
        callback_query={  # type: ignore[arg-type]
            "id": "cb-1",
            "from": {"id": OWNER, "is_bot": False, "first_name": "Some"},
            "chat_instance": "ci-1",
            "data": AdminCallback(action="approve", user_id=MEMBER).pack(),
            "message": {
                "message_id": 2,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": OWNER, "type": "private"},
                "text": "request",
            },
        },
    )
    response = await feed(dispatcher, update)
    assert reached_a_handler(response), "the Approve button reached no handler"


# --------------------------------------------------------------------------- #
# The negatives M8.1 already had, re-asserted through the real dispatcher
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", sorted(OWNER_COMMAND_ARGS))
async def test_owner_commands_stay_silent_for_a_member(
    dispatcher: Dispatcher, command: str
) -> None:
    """The fix must not widen access. A member still matches no handler at all.

    Silence, not a refusal: a member typing ``/approve`` learns nothing — not that the
    command exists, not that they lack the role (M8.1 decision 5).
    """
    text = f"/{command}{OWNER_COMMAND_ARGS[command]}"
    response = await feed(dispatcher, message_update(text, user_id=MEMBER))
    assert not reached_a_handler(response), f"/{command} was handled for a member"


@pytest.mark.parametrize("user_id", [OWNER, MEMBER])
async def test_member_commands_still_work_for_both(dispatcher: Dispatcher, user_id: int) -> None:
    """``/help`` worked throughout the outage, and must keep working.

    It is the control: it proves the dispatcher under test is wired and polling-shaped,
    so a failure above is about the admin router rather than about the harness.
    """
    response = await feed(dispatcher, message_update("/help", user_id=user_id))
    assert reached_a_handler(response)


# --------------------------------------------------------------------------- #
# The member half — audit item 2 (journal/M8_2_REPORT.md §1a)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", sorted(MEMBER_COMMAND_ARGS))
async def test_every_member_command_is_reachable_by_a_member(
    dispatcher: Dispatcher, command: str
) -> None:
    """The other half of the surface, covered because M8.3 landed on it.

    §1a's audit listed the member commands as having no positive test against real
    machinery — the same hole that hid the dead admin router, one router over. They
    do not sit behind a root filter today, so nothing is known to be broken; that is
    exactly the state the admin router was believed to be in.
    """
    text = f"/{command}{MEMBER_COMMAND_ARGS[command]}"
    response = await feed(dispatcher, message_update(text, user_id=MEMBER))
    assert reached_a_handler(response), f"/{command} from a member reached no handler"


def test_the_reachability_list_covers_every_advertised_member_command() -> None:
    """The meta-test, now over both halves of the menu.

    ``MEMBER_COMMANDS`` is what ``setMyCommands`` publishes into an approved
    member's ``/`` menu. Pairing it with the owner meta-test means no command can be
    advertised to anybody without something proving it reaches a handler.
    """
    assert set(MEMBER_COMMAND_ARGS) == {command.command for command in MEMBER_COMMANDS}


async def test_request_is_reachable_and_the_owner_button_answers_it(
    dispatcher: Dispatcher, store: FakeStore
) -> None:
    """M8.3 end to end through the real dispatcher: member asks, owner approves.

    The owner half sits behind the same ``OwnerOnly`` root filter that silently
    dropped every admin update on 2026-08-19, so it gets the same treatment as the
    commands: a positive test, against the real machinery.
    """
    asked = await feed(dispatcher, message_update("/request ATOMUSDT", user_id=MEMBER))
    assert reached_a_handler(asked)
    assert "ATOMUSDT" in store.watchlist_requests, "the request was not stored"

    update = Update(
        update_id=3,
        callback_query={  # type: ignore[arg-type]
            "id": "cb-wl",
            "from": {"id": OWNER, "is_bot": False, "first_name": "Some"},
            "chat_instance": "ci-1",
            "data": WatchlistCallback(symbol="ATOMUSDT", action=AdminAction.APPROVE).pack(),
            "message": {
                "message_id": 3,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": OWNER, "type": "private"},
                "text": "request",
            },
        },
    )
    answered = await feed(dispatcher, update)
    assert reached_a_handler(answered), "the watchlist Approve button reached no handler"
    assert "ATOMUSDT" not in store.watchlist_requests, "the request is still pending"
    assert "ATOMUSDT" in store.settings["watchlist"]


async def test_the_watchlist_button_is_silent_for_a_member(dispatcher: Dispatcher) -> None:
    """It decides what the owner pays for, so it is owner-only like every admin button."""
    update = Update(
        update_id=4,
        callback_query={  # type: ignore[arg-type]
            "id": "cb-wl2",
            "from": {"id": MEMBER, "is_bot": False, "first_name": "Some"},
            "chat_instance": "ci-2",
            "data": WatchlistCallback(symbol="ATOMUSDT", action=AdminAction.APPROVE).pack(),
            "message": {
                "message_id": 4,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": MEMBER, "type": "private"},
                "text": "request",
            },
        },
    )
    assert not reached_a_handler(await feed(dispatcher, update))
