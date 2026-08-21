"""The re-authentication alert, driven to a real outbox (FOREX.md §3.1, M10d).

**Three of the four pieces of the OAuth chain had no caller.** ``SaxoAuth`` itself is
right and has been since M10b — persist inside the operation that spends the token,
assert the write round-tripped, accept any 2xx, read every lifetime — but
``bootstrap()`` and ``ensure_fresh()`` were called from nowhere in ``sentinel/``, and
``reauth_alert()`` was constructed only in a unit test. So the chain could not be
seeded, was not being renewed, and could not say when it died.

HANDOFF §4 item 1 again: *a positive reachability test on real machinery.* Nothing
below asserts a return value. Each test forces a real failure through a real
``SaxoAuth`` over a real ``HttpFetcher`` against a scripted transport, and then reads
the **text that landed in the bot's outbox** — because the whole point of §3.1 is that
the message must name a two-minute browser login rather than say "forex paused".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sentinel.bot.notices import UserNotifier
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings
from sentinel.core.forex_auth import ForexCredentialKeeper, reauth_key
from sentinel.fx.models import SaxoTokenBundle
from sentinel.ingestion.clients.saxo_auth import SaxoAuth
from tests.bot_double import FakeBot, FakeDatabase, FakeMessageStore, FakeStore
from tests.ingestion.conftest import make_fetcher

OWNER = 7222549221
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
LIVE_REFRESH = "a-live-refresh-token"

pytestmark = pytest.mark.asyncio


class MemoryStore:
    """The token store, in memory. ``save`` and ``load`` are both real round trips —
    which is the point, since §3 requirement 2's read-back check is only worth
    anything if it actually reads."""

    def __init__(self, bundle: SaxoTokenBundle | None = None) -> None:
        self.bundle = bundle

    async def load(self) -> SaxoTokenBundle | None:
        return self.bundle

    async def save(self, bundle: SaxoTokenBundle) -> None:
        self.bundle = bundle


def stored(refresh: str = LIVE_REFRESH) -> SaxoTokenBundle:
    """A credential old enough that the five-minute cadence is due.

    ``ensure_fresh`` holds off until ``obtained_at + token_refresh_interval_seconds``,
    so a bundle stamped *now* makes every tick a no-op — which would have made every
    assertion in this file pass for the wrong reason.
    """
    return SaxoTokenBundle(
        refresh_token=SecretStr(refresh), obtained_at=NOW - timedelta(minutes=10)
    )


def refusing(status: int = 400) -> httpx.MockTransport:
    """Saxo refusing the refresh token. A 4xx here means the credential itself is the
    problem — spent, revoked, or rotated out from under us — and no retry fixes it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "invalid_grant"})

    return httpx.MockTransport(handler)


def keeper(
    config: AppConfig,
    transport: httpx.MockTransport,
    store: MemoryStore,
    bot: FakeBot,
    *,
    owner_id: int | None = OWNER,
) -> tuple[ForexCredentialKeeper, httpx.AsyncClient]:
    """The real keeper over the real ``SaxoAuth``. Only the socket is doubled.

    ``forex_auth`` is replaced rather than ``httpx.AsyncClient`` so that the object
    under test is the genuine one: the same rules, the same
    ``ReauthenticationRequired``, the same round-trip assertion.
    """
    fetcher, client = make_fetcher(transport, max_retries=0)
    auth = SaxoAuth(
        config.forex,
        fetcher=fetcher,
        store=store,
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
        clock=FrozenClock(NOW),
    )

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_forex_auth(*_: Any, **__: Any) -> Any:
        yield auth

    database = FakeDatabase(FakeStore())
    notices = UserNotifier(
        database,  # type: ignore[arg-type]
        bot,
        telegram=config.telegram,
        clock=FrozenClock(NOW),
        messages=FakeMessageStore,
    )
    settings = Settings(
        secrets=Secrets(_env_file=None, telegram_owner_user_id=owner_id), config=config
    )
    built = ForexCredentialKeeper(
        settings,
        database,  # type: ignore[arg-type]
        notices=notices,
        clock=FrozenClock(NOW),
        auth_factory=fake_forex_auth,
    )
    return built, client


def outbox(bot: FakeBot) -> list[str]:
    return [call.kwargs["text"] for call in bot.calls if call.method == "send_message"]


# --------------------------------------------------------------------------- #
# The alert reaches a chat, and it names the job
# --------------------------------------------------------------------------- #


async def test_a_dead_chain_tells_the_owner_to_log_in_again_and_carries_the_url(
    repo_config: AppConfig,
) -> None:
    """§3.1's whole requirement, asserted on the text rather than on an exception.

    "Forex paused" would send the owner hunting a data problem. What he actually has
    is a two-minute browser login, so the message has to say that and carry the
    address — and until M10d nothing sent this message at all.
    """
    bot = FakeBot()
    keep, client = keeper(repo_config, refusing(), MemoryStore(stored()), bot)
    async with client:
        assert await keep.tick() is False

    sent = outbox(bot)
    assert len(sent) == 1, sent
    text = sent[0]
    assert "log in" in text.lower()
    assert repo_config.forex.authorize_url in text
    assert "Crypto is unaffected" in text


async def test_a_healthy_chain_sends_nothing(repo_config: AppConfig) -> None:
    """The sibling that gives every assertion here teeth: an alerter that fired on
    every tick would satisfy all of them."""

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 1182,
                "refresh_token_expires_in": 3582,
                "token_type": "Bearer",
            },
        )

    bot = FakeBot()
    keep, client = keeper(repo_config, httpx.MockTransport(ok), MemoryStore(stored()), bot)
    async with client:
        assert await keep.tick() is True

    assert outbox(bot) == []


async def test_a_vendor_outage_is_not_a_dead_credential(repo_config: AppConfig) -> None:
    """A 5xx is Saxo having a bad minute, and the owner has nothing to do about it.

    Alerting on it would train him to ignore the one message that does need him — the
    same reasoning that makes the calendar alert daily rather than hourly.
    """
    bot = FakeBot()
    keep, client = keeper(repo_config, refusing(status=503), MemoryStore(stored()), bot)
    async with client:
        assert await keep.tick() is False

    assert outbox(bot) == []


async def test_an_empty_store_is_a_reauth_and_says_so(repo_config: AppConfig) -> None:
    """The state a fresh server is in before the browser login. Not an error to debug
    — a job to do, and the alert is how the owner learns which."""
    bot = FakeBot()
    keep, client = keeper(repo_config, refusing(), MemoryStore(None), bot)
    async with client:
        assert await keep.tick() is False

    assert "log in" in outbox(bot)[0].lower()


# --------------------------------------------------------------------------- #
# One alert per DEAD CREDENTIAL, not one per day
# --------------------------------------------------------------------------- #


async def test_a_repeated_tick_on_the_same_dead_chain_sends_once(
    repo_config: AppConfig,
) -> None:
    """Five minutes between ticks is 288 messages a day. The claim is what stops it."""
    bot = FakeBot()
    keep, client = keeper(repo_config, refusing(), MemoryStore(stored()), bot)
    async with client:
        await keep.tick()
        await keep.tick()
        await keep.tick()

    assert len(outbox(bot)) == 1


async def test_a_second_death_after_a_fresh_login_alerts_again(repo_config: AppConfig) -> None:
    """The reason the key is a fingerprint and not a date.

    He logs in at 10:00, the chain lives, the container is down again past 15:00. A
    date-keyed alert would suppress the message he needs most, on the day he has
    already proved he responds to it.
    """
    assert reauth_key("abc") != reauth_key("def")
    bot = FakeBot()
    store = MemoryStore(stored("first-chain"))
    keep, client = keeper(repo_config, refusing(), store, bot)
    async with client:
        await keep.tick()
        # The owner logs in again: a brand-new credential, and therefore a new key.
        store.bundle = stored("second-chain-after-a-manual-login")
        await keep.tick()

    assert len(outbox(bot)) == 2


async def test_without_an_owner_configured_it_logs_and_sends_nothing(
    repo_config: AppConfig,
) -> None:
    """A degraded start is not a crash. There is nowhere to send it, so it is logged
    and the tick still returns cleanly rather than raising into the scheduler."""
    bot = FakeBot()
    keep, client = keeper(repo_config, refusing(), MemoryStore(stored()), bot, owner_id=None)
    async with client:
        assert await keep.tick() is False

    assert outbox(bot) == []


# --------------------------------------------------------------------------- #
# The seeder
# --------------------------------------------------------------------------- #


async def test_the_bootstrap_token_reaches_the_store_at_boot(repo_config: AppConfig) -> None:
    """Nothing called ``bootstrap()`` until M10d, so SAXO_REFRESH_TOKEN never left
    ``.env`` — the store was empty on the first cycle and forex was dead from its
    first minute with a valid credential a metre away."""
    bot = FakeBot()
    store = MemoryStore(None)
    keep, client = keeper(repo_config, refusing(), store, bot)
    keep._settings = keep._settings.model_copy(
        update={
            "secrets": Secrets(
                _env_file=None,
                telegram_owner_user_id=OWNER,
                saxo_refresh_token=SecretStr("from-the-browser-login"),
            )
        }
    )
    async with client:
        assert await keep.seed() is True

    assert store.bundle is not None
    assert store.bundle.refresh_token_value == "from-the-browser-login"


async def test_seeding_never_overwrites_a_live_stored_credential(
    repo_config: AppConfig,
) -> None:
    """The property that makes seeding-on-every-boot safe, and it belongs to
    ``bootstrap()`` rather than to the call site remembering to be careful.

    The ``.env`` token is single-use and was spent on the first refresh, so a redeploy
    that overwrote the live chain with it would END the chain and force exactly the
    manual login this exists to avoid.
    """
    bot = FakeBot()
    store = MemoryStore(stored("the-live-rotated-one"))
    keep, client = keeper(repo_config, refusing(), store, bot)
    keep._settings = keep._settings.model_copy(
        update={
            "secrets": Secrets(
                _env_file=None,
                telegram_owner_user_id=OWNER,
                saxo_refresh_token=SecretStr("the-stale-env-one"),
            )
        }
    )
    async with client:
        assert await keep.seed() is True

    assert store.bundle is not None
    assert store.bundle.refresh_token_value == "the-live-rotated-one"
