"""The Saxo OAuth chain — docs/specs/FOREX.md §3 and failure mode D.

Every fact these tests encode is measured, from journal/M10b_SPIKE.md §1: the token
exchange answers **201**, lifetimes are 1070/1182/1200 and 3582/3600 seconds and vary
between responses, and the refresh token **rotates on every use**.

The two tests that matter most are the ones about the write rather than the call:
``test_a_persist_that_silently_does_nothing_is_caught`` and
``test_a_persist_that_stores_something_else_is_caught``. A 2xx that is not saved is
indistinguishable from success at the HTTP layer, and because the refresh token is
single-use it does not merely leave the system stale — it ends the chain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog
from pydantic import SecretStr

from sentinel.core.alerts import AlertKind, reauth_alert
from sentinel.core.clock import FrozenClock
from sentinel.core.config import ForexConfig
from sentinel.fx.errors import ReauthenticationRequired, TokenPersistFailed
from sentinel.fx.models import SaxoTokenBundle, fingerprint
from sentinel.ingestion.clients.saxo_auth import SaxoAuth
from sentinel.ingestion.errors import SourceUnavailable
from tests.ingestion.conftest import make_fetcher

NOW = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)
APP_KEY = SecretStr("app-key")
APP_SECRET = SecretStr("app-secret")

#: The token values used throughout. Distinct strings so rotation is observable and
#: so a leak into a log line is greppable.
SEED_REFRESH = "seed-refresh-token-aaaa"
NEW_REFRESH = "rotated-refresh-token-bbbb"
NEW_ACCESS = "issued-access-token-cccc"


class MemoryStore:
    """An honest store: what goes in comes back out."""

    def __init__(self, bundle: SaxoTokenBundle | None = None) -> None:
        self.bundle = bundle
        self.saves = 0

    async def load(self) -> SaxoTokenBundle | None:
        return self.bundle

    async def save(self, bundle: SaxoTokenBundle) -> None:
        self.saves += 1
        self.bundle = bundle


class SilentlyDroppingStore(MemoryStore):
    """Accepts every write and keeps the *previous* value — a failed UPDATE.

    The spike's bug as a test double, and in its most realistic shape: the store is
    not empty afterwards, it is stale. Nothing about the call says so.
    """

    async def save(self, bundle: SaxoTokenBundle) -> None:
        self.saves += 1


class VanishingStore(MemoryStore):
    """Accepts every write and has nothing at all afterwards."""

    async def save(self, bundle: SaxoTokenBundle) -> None:
        self.saves += 1
        self.bundle = None


class RewritingStore(MemoryStore):
    """Stores *something*, but not what it was given — a truncating column, say."""

    async def save(self, bundle: SaxoTokenBundle) -> None:
        self.saves += 1
        self.bundle = bundle.model_copy(update={"refresh_token": SecretStr("truncated")})


def seed(*, refresh_expires_at: datetime | None = None, count: int = 0) -> SaxoTokenBundle:
    return SaxoTokenBundle(
        refresh_token=SecretStr(SEED_REFRESH),
        obtained_at=NOW - timedelta(minutes=6),
        refresh_expires_at=refresh_expires_at,
        refresh_count=count,
    )


def token_response(
    *,
    status: int = 201,
    expires_in: int | None = 1182,
    refresh_expires_in: int | None = 3582,
    access: str | None = NEW_ACCESS,
    refresh: str | None = NEW_REFRESH,
) -> httpx.MockTransport:
    body: dict[str, Any] = {"token_type": "Bearer", "base_uri": None}
    if access is not None:
        body["access_token"] = access
    if refresh is not None:
        body["refresh_token"] = refresh
    if expires_in is not None:
        body["expires_in"] = expires_in
    if refresh_expires_in is not None:
        body["refresh_token_expires_in"] = refresh_expires_in

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def build(
    transport: httpx.MockTransport,
    store: MemoryStore,
    *,
    now: datetime = NOW,
    config: ForexConfig | None = None,
) -> tuple[SaxoAuth, httpx.AsyncClient]:
    fetcher, client = make_fetcher(transport, max_retries=0)
    auth = SaxoAuth(
        config or ForexConfig(),
        fetcher=fetcher,
        store=store,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        clock=FrozenClock(now),
    )
    return auth, client


# ── the status code (spike defect C1) ───────────────────────────────────────


@pytest.mark.parametrize("status", [200, 201, 202], ids=["200", "201-saxo", "202"])
async def test_any_2xx_is_a_successful_exchange(status: int) -> None:
    """Saxo answers **201**. The spike checked ``!= 200`` and sent a successful
    exchange down the error branch: the credential was issued and thrown away."""
    store = MemoryStore(seed())
    auth, client = build(token_response(status=status), store)
    async with client:
        bundle = await auth.refresh()
    assert bundle.access_token_value == NEW_ACCESS
    assert store.bundle is not None
    assert store.bundle.refresh_token_value == NEW_REFRESH


# ── rotation and persistence (§3 requirements 1 and 2) ──────────────────────


async def test_the_rotated_refresh_token_is_persisted_and_read_back() -> None:
    store = MemoryStore(seed())
    auth, client = build(token_response(), store)
    async with client:
        bundle = await auth.refresh()

    assert store.saves == 1
    assert bundle.refresh_fingerprint != seed().refresh_fingerprint, "it must rotate"
    assert store.bundle is not None
    assert store.bundle.refresh_fingerprint == bundle.refresh_fingerprint
    assert bundle.refresh_count == 1


async def test_a_persist_that_silently_does_nothing_is_caught() -> None:
    """The exact spike failure: a 2xx, a success log, and nothing saved.

    Because the refresh token is single-use, this does not leave the system stale —
    it ends the chain. So it has to raise here, while there is still something to say
    about it, rather than 20 minutes later as a mysterious 401.
    """
    store = SilentlyDroppingStore(seed())
    auth, client = build(token_response(), store)
    async with client:
        with pytest.raises(TokenPersistFailed, match="did not round-trip"):
            await auth.refresh()
    assert store.saves == 1, "the write was attempted and reported success"
    assert store.bundle is not None
    assert store.bundle.refresh_token_value == SEED_REFRESH, (
        "the store still holds the spent token, which is what makes this invisible "
        "to anything that only checks the HTTP status"
    )


async def test_a_persist_that_leaves_the_store_empty_is_caught() -> None:
    store = VanishingStore(seed())
    auth, client = build(token_response(), store)
    async with client:
        with pytest.raises(TokenPersistFailed, match="read back empty"):
            await auth.refresh()


async def test_a_persist_that_stores_something_else_is_caught() -> None:
    store = RewritingStore(seed())
    auth, client = build(token_response(), store)
    async with client:
        with pytest.raises(TokenPersistFailed, match="refresh_token"):
            await auth.refresh()


# ── lifetimes are read, never assumed (spike defect D2) ─────────────────────


@pytest.mark.parametrize(
    ("expires_in", "refresh_expires_in"),
    [(1070, 3582), (1182, 3582), (1200, 3600)],
    ids=["1070", "1182", "1200"],
)
async def test_every_lifetime_comes_from_the_response(
    expires_in: int, refresh_expires_in: int
) -> None:
    """Observed values, not round numbers, and different between responses. A
    hardcoded constant would be wrong by construction."""
    store = MemoryStore(seed())
    auth, client = build(
        token_response(expires_in=expires_in, refresh_expires_in=refresh_expires_in), store
    )
    async with client:
        bundle = await auth.refresh()

    assert bundle.access_expires_at == NOW + timedelta(seconds=expires_in)
    assert bundle.refresh_expires_at == NOW + timedelta(seconds=refresh_expires_in)


async def test_a_missing_refresh_lifetime_means_unknown_not_never() -> None:
    """ "Unknown" and "does not expire" are the same value and opposite facts. Guessing
    the second turns a dead chain into one that looks alive until everything 401s."""
    store = MemoryStore(seed())
    auth, client = build(token_response(refresh_expires_in=None), store)
    async with client:
        bundle = await auth.refresh()

    assert bundle.refresh_expires_at is None
    assert bundle.refresh_is_dead(NOW + timedelta(days=365)) is False, (
        "unknown must not be treated as dead either — the only honest way to find "
        "out whether a bootstrap token still works is to use it"
    )


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"access": None}, "access_token"),
        ({"refresh": None}, "refresh_token"),
        ({"expires_in": None}, "expires_in"),
    ],
)
async def test_a_2xx_missing_a_required_field_is_not_a_successful_refresh(
    kwargs: dict[str, Any], missing: str
) -> None:
    store = MemoryStore(seed())
    auth, client = build(token_response(**kwargs), store)
    async with client:
        with pytest.raises(SourceUnavailable, match=missing):
            await auth.refresh()
    assert store.saves == 0


# ── failing loudly, and specifically (§3.1, §12) ────────────────────────────


async def test_an_expired_refresh_chain_asks_for_re_authentication() -> None:
    """§3.1's one-hour memory. Any outage longer than that ends here."""
    store = MemoryStore(seed(refresh_expires_at=NOW - timedelta(minutes=1)))
    auth, client = build(token_response(), store)
    async with client:
        with pytest.raises(ReauthenticationRequired, match="one-hour memory"):
            await auth.refresh()


async def test_a_4xx_is_re_authentication_and_not_a_retry() -> None:
    """A rejected refresh token cannot be un-rejected by asking again."""
    store = MemoryStore(seed())
    auth, client = build(token_response(status=400), store)
    async with client:
        with pytest.raises(ReauthenticationRequired, match="single-use"):
            await auth.refresh()


async def test_a_5xx_is_the_vendor_and_stays_a_source_failure() -> None:
    """Forex pauses and retries; it does not send the owner to a login page."""
    store = MemoryStore(seed())
    auth, client = build(token_response(status=503), store)
    async with client:
        with pytest.raises(SourceUnavailable):
            await auth.refresh()


async def test_an_empty_store_asks_for_a_login_rather_than_failing_obscurely() -> None:
    store = MemoryStore(None)
    auth, client = build(token_response(), store)
    async with client:
        with pytest.raises(ReauthenticationRequired, match="manual browser login"):
            await auth.refresh()


def test_the_alert_names_the_action_and_carries_the_url() -> None:
    """§3.1. A generic "forex paused" sends the owner hunting a data problem when
    the fix is a two-minute browser login."""
    from sentinel.bot.readmodels import alert_view

    alert = reauth_alert(
        authorize_url="https://live.logonvalidation.net/authorize?client_id=…",
        detail="the refresh token expired at 2026-08-21T06:00:00+00:00",
    )
    assert alert.kind is AlertKind.FOREX_REAUTH_REQUIRED

    view = alert_view(alert)
    body = "\n".join(view.body)
    assert "log in" in view.title.lower()
    assert "browser login" in body
    assert "https://live.logonvalidation.net/authorize" in body
    assert "Crypto is unaffected" in body


# ── bootstrap: the stored credential always wins ────────────────────────────


async def test_bootstrap_seeds_an_empty_store_with_an_unknown_expiry() -> None:
    store = MemoryStore(None)
    auth, client = build(token_response(), store)
    async with client:
        bundle = await auth.bootstrap(SecretStr(SEED_REFRESH))

    assert bundle.refresh_token_value == SEED_REFRESH
    assert bundle.refresh_expires_at is None
    assert bundle.access_token is None
    assert store.saves == 1


async def test_bootstrap_never_overwrites_a_live_stored_credential() -> None:
    """A redeploy must not rewind the chain to the ``.env`` token.

    That value was single-use and was spent the first time the service refreshed.
    Letting it win would end the chain and force the very login it is meant to avoid
    — §3 requirement 1 arriving from an unexpected direction.
    """
    live = SaxoTokenBundle(refresh_token=SecretStr(NEW_REFRESH), obtained_at=NOW, refresh_count=17)
    store = MemoryStore(live)
    auth, client = build(token_response(), store)
    async with client:
        bundle = await auth.bootstrap(SecretStr(SEED_REFRESH))

    assert bundle.refresh_token_value == NEW_REFRESH
    assert bundle.refresh_count == 17
    assert store.saves == 0


# ── the cadence (§3 requirement 5) ──────────────────────────────────────────


async def test_a_valid_access_token_is_reused_rather_than_refreshed() -> None:
    store = MemoryStore(
        seed().model_copy(
            update={
                "access_token": SecretStr("still-good"),
                "access_expires_at": NOW + timedelta(minutes=10),
            }
        )
    )
    auth, client = build(token_response(), store)
    async with client:
        assert await auth.access_token() == "still-good"
    assert store.saves == 0


async def test_a_token_inside_the_expiry_margin_is_refreshed_before_it_is_used() -> None:
    """A request that starts valid and arrives expired looks, from here, exactly like
    a credential that was never any good."""
    margin = ForexConfig().token_expiry_margin_seconds
    store = MemoryStore(
        seed().model_copy(
            update={
                "access_token": SecretStr("about-to-die"),
                "access_expires_at": NOW + timedelta(seconds=margin - 1),
            }
        )
    )
    auth, client = build(token_response(), store)
    async with client:
        assert await auth.access_token() == NEW_ACCESS
    assert store.saves == 1


async def test_ensure_fresh_holds_off_until_the_cadence_is_due() -> None:
    store = MemoryStore(seed().model_copy(update={"obtained_at": NOW - timedelta(minutes=1)}))
    auth, client = build(token_response(), store)
    async with client:
        assert await auth.ensure_fresh() is None
    assert store.saves == 0


async def test_ensure_fresh_refreshes_on_the_cadence_even_with_nobody_asking() -> None:
    """The five-minute job exists for the refresh token's one-hour life, not for the
    access token — so it runs all weekend, when no chart is being read at all."""
    store = MemoryStore(seed().model_copy(update={"obtained_at": NOW - timedelta(minutes=6)}))
    auth, client = build(token_response(), store)
    async with client:
        bundle = await auth.ensure_fresh()
    assert bundle is not None
    assert store.saves == 1


# ── credentials never reach a log line ──────────────────────────────────────


async def test_no_token_value_ever_appears_in_a_log_line() -> None:
    """Fingerprints only. The same discipline the spike used to compare two runs'
    credentials without exposing either."""
    store = MemoryStore(seed())
    auth, client = build(token_response(), store)
    with structlog.testing.capture_logs() as logs:
        async with client:
            await auth.bootstrap(SecretStr(SEED_REFRESH))
            await auth.refresh()

    rendered = repr(logs)
    for secret in (SEED_REFRESH, NEW_REFRESH, NEW_ACCESS, "app-key", "app-secret"):
        assert secret not in rendered, f"{secret} leaked into a log line"

    refreshed = [entry for entry in logs if entry["event"] == "forex.auth_refreshed"]
    assert refreshed and refreshed[0]["refresh_rotated"] is True
    assert refreshed[0]["refresh_after"] == fingerprint(SecretStr(NEW_REFRESH))
