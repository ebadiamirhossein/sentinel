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
from sentinel.bot.handlers.guard import FAILED
from sentinel.bot.keyboards import AdminAction, AdminCallback, WatchlistCallback
from sentinel.bot.menu import MEMBER_COMMANDS, OWNER_COMMANDS
from sentinel.bot.runtime import watchlist_key
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
#: ``/pulse`` is here because it is on the member menu; it is the one entry both
#: roles may run, and it gets its own both-roles test below.
MEMBER_COMMAND_ARGS: dict[str, str] = {
    "help": "",
    "capital": " 5000",
    "risk": " 0.75",
    "positions": "",
    "stats": "",
    "pulse": "",
    "snapshot": " SOLUSDT",
    "journal": "",
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
        #: What a handler actually said. Recorded from M8.6, because reachability
        #: alone stopped being a sufficient assertion once every member handler
        #: gained a catch-all — see the test at the foot of this file.
        self.texts: list[str] = []

    async def __call__(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        """aiogram's own call path: handlers build a method object and await the bot."""
        self.calls.append(type(method).__name__)
        text = getattr(method, "text", None)
        if text is not None:
            self.texts.append(text)
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
    assert "ATOMUSDT" in store.settings[watchlist_key()]


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


# --------------------------------------------------------------------------- #
# /pulse — the one command both roles run, so both roles are proven (M8.4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_pulse_is_reachable_by_both_roles(dispatcher: Dispatcher, user_id: int) -> None:
    """Every other command in this bot belongs to exactly one role. ``/pulse`` belongs
    to both, and "both" is the claim that needs proving against the real machinery.

    The member half rides on ``MEMBER_COMMAND_ARGS``' parametrized sweep. The owner
    half does not: ``commands_router`` is registered *before* ``admin_router``, so a
    future owner-only handler named ``pulse`` — or an ``OwnerOnly`` filter added to
    the wrong router — would shadow or silence this one, and nothing in the owner
    sweep would notice, because ``/pulse`` is not on ``OWNER_COMMANDS``.

    Against a real ``Dispatcher``, because that is where the shadowing would happen:
    journal/M8_2_REPORT.md §1a's rule is that a path whose failure mode is silence
    needs a positive test, and it has to run the way production runs.
    """
    response = await feed(dispatcher, message_update("/pulse", user_id=user_id))
    assert reached_a_handler(response), f"/pulse reached no handler for {user_id}"


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_pulse_24h_is_reachable_by_both_roles(dispatcher: Dispatcher, user_id: int) -> None:
    """The argument is a different query path — ``completed_since`` and an aggregate
    rather than one cycle — so it gets its own assertion rather than riding on the
    bare form's."""
    response = await feed(dispatcher, message_update("/pulse 24h", user_id=user_id))
    assert reached_a_handler(response), f"/pulse 24h reached no handler for {user_id}"


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_an_unrecognised_pulse_window_is_answered_rather_than_ignored(
    dispatcher: Dispatcher, user_id: int
) -> None:
    """``/pulse 7d`` must not silently render the last cycle: a reader would take one
    cycle's quiet for a week's."""
    response = await feed(dispatcher, message_update("/pulse 7d", user_id=user_id))
    assert reached_a_handler(response)


async def test_pulse_is_silent_for_someone_with_no_standing(dispatcher: Dispatcher) -> None:
    """It is a read of the pipeline, not of a book — and still behind the gate.

    specs/TELEGRAM_UX.md §1: nothing but ``/start`` answers an unknown id, because a
    reply confirms a live private bot is there. A transparency command is exactly the
    kind of thing that looks harmless enough to route around the gate.

    Asserted on **outbound calls**, not on :func:`reached_a_handler`. The gate is an
    outer middleware and a ``DROP`` returns ``None`` from it, which
    ``feed_update`` hands back verbatim — indistinguishable from a handler that ran
    and returned nothing. What is observable, and what actually matters, is that the
    stranger's phone stayed quiet.
    """
    bot = FakeBot()
    await dispatcher.feed_update(bot, message_update("/pulse", user_id=999))  # type: ignore[arg-type]
    assert bot.calls == [], f"a stranger's /pulse produced {bot.calls}"


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_pulse_for_one_symbol_is_reachable_by_both_roles(
    dispatcher: Dispatcher, user_id: int
) -> None:
    """The third argument shape, and a third query path — ``latest_for_symbol`` plus a
    watchlist read — so it gets its own assertion for both roles (M8.5)."""
    response = await feed(dispatcher, message_update("/pulse SOLUSDT", user_id=user_id))
    assert reached_a_handler(response), f"/pulse SOLUSDT reached no handler for {user_id}"


async def test_a_day_word_is_never_taken_for_a_symbol(dispatcher: Dispatcher) -> None:
    """The two vocabularies cannot collide — ``parse_symbol`` needs five alphanumeric
    characters and every day word is shorter — but the *order* is what keeps that
    decision in one place, so it is asserted rather than left to the coincidence.

    A regression here is quiet in the worst way: ``/pulse 24h`` would fall through to
    the symbol branch and answer "24H is not on the watchlist", which reads as a
    broken command rather than as a routing bug.
    """
    bot = FakeBot()
    await dispatcher.feed_update(bot, message_update("/pulse 24h", user_id=MEMBER))  # type: ignore[arg-type]
    assert bot.calls, "the day form answered nothing at all"


async def test_an_unparseable_pulse_argument_gets_the_usage_line(
    dispatcher: Dispatcher,
) -> None:
    """``/pulse !!`` is neither a window nor a symbol. It must be answered, not
    silently treated as the bare form."""
    response = await feed(dispatcher, message_update("/pulse !!", user_id=MEMBER))
    assert reached_a_handler(response)


# --------------------------------------------------------------------------- #
# M8.6 — /snapshot and /journal, and the roles that may reach them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_snapshot_is_reachable_by_both_roles(dispatcher: Dispatcher, user_id: int) -> None:
    """Shared market data, so both halves of the menu must reach it.

    The member half also rides on ``MEMBER_COMMAND_ARGS``' sweep; the owner half does
    not exist anywhere else, because ``/snapshot`` is deliberately not on
    ``OWNER_COMMANDS`` — putting it there would take it off every member's menu
    (M8.4 decision 5). ``commands_router`` is registered before ``admin_router``, so
    an ``OwnerOnly`` filter added to the wrong router would silence this for a
    member and nothing in the owner sweep would notice.
    """
    assert reached_a_handler(
        await feed(dispatcher, message_update("/snapshot SOLUSDT", user_id=user_id))
    )


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_snapshot_with_no_symbol_is_answered_rather_than_ignored(
    dispatcher: Dispatcher, user_id: int
) -> None:
    """A bare ``/snapshot`` gets the usage line. Silence would be indistinguishable
    from the command not existing — which is what silence *means* everywhere else in
    this bot, and must not mean here."""
    assert reached_a_handler(await feed(dispatcher, message_update("/snapshot", user_id=user_id)))


async def test_snapshot_is_silent_for_someone_with_no_standing(dispatcher: Dispatcher) -> None:
    """Asserted on outbound calls rather than on ``UNHANDLED``: the gate is an outer
    middleware and a ``DROP`` returns ``None``, which ``feed_update`` hands back
    verbatim — indistinguishable from a handler that ran and returned nothing. What
    is observable, and what matters, is that the stranger's phone stayed quiet."""
    bot = FakeBot()
    await dispatcher.feed_update(bot, message_update("/snapshot SOLUSDT", user_id=999))  # type: ignore[arg-type]
    assert bot.calls == []


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_journal_is_reachable_by_both_roles(dispatcher: Dispatcher, user_id: int) -> None:
    assert reached_a_handler(await feed(dispatcher, message_update("/journal", user_id=user_id)))


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_journal_with_a_window_is_reachable_by_both_roles(
    dispatcher: Dispatcher, user_id: int
) -> None:
    """The windowed form takes a different path through the handler — it resolves a
    cutoff where the bare form does not — so it is exercised separately."""
    assert reached_a_handler(
        await feed(dispatcher, message_update("/journal 30d", user_id=user_id))
    )


@pytest.mark.parametrize("user_id", [OWNER, MEMBER], ids=["owner", "member"])
async def test_an_unrecognised_journal_window_is_answered_rather_than_ignored(
    dispatcher: Dispatcher, user_id: int
) -> None:
    """``/journal 7d`` gets usage, not a silent fall back to the whole history.

    The regression this guards is quiet in the worst way: somebody asking for a week
    and receiving two years would read the file as a week's worth.
    """
    assert reached_a_handler(await feed(dispatcher, message_update("/journal 7d", user_id=user_id)))


async def test_journal_is_silent_for_someone_with_no_standing(dispatcher: Dispatcher) -> None:
    bot = FakeBot()
    await dispatcher.feed_update(bot, message_update("/journal", user_id=999))  # type: ignore[arg-type]
    assert bot.calls == []


# --------------------------------------------------------------------------- #
# M8.6 — the catch-all must not turn a broken handler into a passing test
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", sorted(MEMBER_COMMAND_ARGS))
async def test_a_member_command_does_its_own_job_rather_than_apologising(
    dispatcher: Dispatcher, command: str
) -> None:
    """Reachability stopped being enough the moment ``answers_on_failure`` existed.

    Every member handler now replies on failure instead of dying quietly, which is
    the point — and it means ``reached_a_handler`` would go on passing if aiogram
    stopped injecting ``ctx``, ``actor`` or ``command``: the resulting ``TypeError``
    would be caught by the guard and answered. The sweep above would be green over a
    completely broken router, which is precisely the M8.2 failure it exists to
    prevent, reintroduced by its own fix.

    So the assertion is on what the handler *said*: anything but the apology.
    """
    bot = FakeBot()
    update = message_update(f"/{command}{MEMBER_COMMAND_ARGS[command]}", user_id=MEMBER)
    await dispatcher.feed_update(bot, update)  # type: ignore[arg-type]

    assert bot.texts, f"/{command} reached a handler and said nothing at all"
    assert FAILED not in bot.texts, (
        f"/{command} failed and apologised — the guard is working and the handler is "
        "not. The traceback is in the captured log."
    )


# --------------------------------------------------------------------------- #
# M10a — the market-argument forms, through the real Dispatcher
# --------------------------------------------------------------------------- #

#: Every command that grew an optional market argument at M10a. Each is a *new
#: dispatch path*: three of these handlers gained a ``CommandObject`` parameter they
#: did not take before, and aiogram resolves handler arguments by name at call time
#: — so a signature the dispatcher cannot satisfy fails at runtime, in production,
#: as silence. journal/M8_2_REPORT.md §1a's rule, applied to the new arguments.
MARKET_ARGUMENT_COMMANDS = (
    "/status crypto",
    "/pause crypto",
    "/resume crypto",
    "/watchlist crypto",
    "/pulse crypto",
    "/stats crypto",
)


@pytest.mark.parametrize("text", MARKET_ARGUMENT_COMMANDS)
async def test_the_market_argument_forms_reach_a_handler(dispatcher: Dispatcher, text: str) -> None:
    response = await feed(dispatcher, message_update(text, user_id=OWNER))
    assert reached_a_handler(response), f"{text} reached no handler"


@pytest.mark.parametrize("text", ["/status", "/pause", "/resume", "/watchlist"])
async def test_the_bare_forms_still_reach_a_handler(dispatcher: Dispatcher, text: str) -> None:
    """The argument is optional, and the bare form is the one the owner's fingers
    know. A required ``CommandObject`` would have broken exactly these."""
    response = await feed(dispatcher, message_update(text, user_id=OWNER))
    assert reached_a_handler(response), f"{text} reached no handler"
