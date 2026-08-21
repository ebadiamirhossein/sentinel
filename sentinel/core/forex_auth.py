"""Keeping the Saxo credential alive, and saying so when it dies (FOREX.md §3, §3.1).

**Every hard part of this was already written and none of it had a caller.**
``SaxoAuth`` persists inside the same operation that spends the token, asserts the
write round-tripped, never reads a literal status code, and stores absolute expiries
read from each response. All of it correct, all of it tested, and at M10c the chain
had **three** unwired pieces out of four (journal/M10d_REPORT.md, P3):

======  =========================================================================
seed    ``bootstrap()`` was called from nowhere. ``SAXO_REFRESH_TOKEN`` sat in
        ``.env`` and never reached Postgres, so the store was empty at boot,
        ``refresh()`` raised "no Saxo credential is stored", and **forex would have
        been dead on its first cycle** with a valid token a metre away.
renew   ``ensure_fresh()`` was called from nowhere. The token was touched only by a
        scan, on a 60-minute interval, against a refresh token that lives ~1 hour.
        Survival by luck.
tell    ``reauth_alert()`` was constructed only in a test, and
        ``ReauthenticationRequired`` was caught nowhere that could send a message.
        The owner would have got silence.
======  =========================================================================

This module is those three callers, and nothing else. It holds no rules: the rules
are in ``SaxoAuth``, and the message is in ``core/alerts`` and ``bot/readmodels``.

**Why the alert is keyed on the dead credential's fingerprint.** A daily key would
suppress the second death of the day — the owner logs in at 10:00, the container is
down again at 15:00, and the message that would tell him never arrives. A fingerprint
gives one alert per dead chain and a fresh one for a fresh chain, which is exactly the
granularity the event has.

**And why §3.1's framing needed correcting.** The one-hour memory reads like constant
toil and is not: the owner's deploys take ~30 s and his last reboot ~90 s, both far
inside the window, and every refresh resets it. This is one manual login after an
outage **longer than an hour** — a rare-event cost, not a recurring one.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from sentinel.bot.cards import alert_card
from sentinel.bot.formatting import zone_info
from sentinel.bot.readmodels import alert_view
from sentinel.core.alerts import reauth_alert
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.core.wiring import DatabaseTokenStore, forex_auth
from sentinel.fx.errors import ReauthenticationRequired, TokenPersistFailed
from sentinel.ingestion.clients.saxo_auth import SaxoAuth
from sentinel.storage.db import Database

log = get_logger(__name__)


class AuthFactory(Protocol):
    """How :class:`ForexCredentialKeeper` gets a chain to work on."""

    def __call__(
        self, settings: Settings, database: Database
    ) -> AbstractAsyncContextManager[SaxoAuth]: ...


class SupportsNotice(Protocol):
    """The one method needed from ``bot.notices.UserNotifier``.

    A protocol rather than the class, so this module does not import a bot to decide
    whether a credential is alive — the same split ``core/alerts`` and ``bot/alerts``
    already draw.
    """

    async def notice(self, user_id: int, *, key: str, text: str) -> bool: ...


def reauth_key(fingerprint: str | None) -> str:
    """One alert per **dead credential**, not one per day.

    A date-keyed alert would go quiet after the first message of the day, which is
    exactly wrong here: the owner logs in, the chain lives again, and if it dies a
    second time that afternoon the message he needs is the one that would be
    suppressed. A fingerprint changes when the credential does.
    """
    return f"forex-reauth:{fingerprint or 'none-stored'}"


class ForexCredentialKeeper:
    """Seeds, renews and — when it cannot — tells the owner exactly what to do."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        notices: SupportsNotice | None = None,
        clock: Clock | None = None,
        auth_factory: AuthFactory = forex_auth,
    ) -> None:
        self._settings = settings
        self._database = database
        self._notices = notices
        self._clock = clock or SystemClock()
        #: How the OAuth chain is built. A seam rather than a module-level lookup,
        #: and for the same reason the cycle takes ``repositories``: the branch that
        #: matters here is the one where the credential is **dead**, and arranging
        #: that against a real ``SaxoAuth`` needs a scripted transport underneath it.
        #: Nothing above the socket is doubled by using this.
        self._auth_factory = auth_factory

    # ── seed ─────────────────────────────────────────────────────────────────

    async def seed(self) -> bool:
        """Put ``.env``'s bootstrap refresh token into Postgres, if the store is empty.

        Safe on every boot, and that safety is a property of ``bootstrap()`` rather
        than of this call site: **the stored credential always wins.** The token in
        ``.env`` was spent the first time the service refreshed, so letting a redeploy
        overwrite the live chain with it would end the chain and force the very login
        this exists to avoid.

        Returns ``True`` when a credential is present afterwards, however it got there.
        """
        secret = self._settings.secrets.saxo_refresh_token
        if secret is None:
            stored = await DatabaseTokenStore(self._database).load()
            if stored is not None:
                return True
            log.warning(
                "forex.no_credential",
                detail=(
                    "no SAXO_REFRESH_TOKEN and nothing stored — forex cannot read a "
                    "single candle until a browser login seeds one (docs/DEPLOY.md §13b)"
                ),
            )
            return False
        try:
            async with self._auth_factory(self._settings, self._database) as auth:
                await auth.bootstrap(secret)
        except (ReauthenticationRequired, TokenPersistFailed) as exc:
            # No fingerprint: seeding failed, so whatever was there is what the alert
            # would have named and we could not read it. "none-stored" is the honest key.
            await self._tell(str(exc))
            return False
        return True

    # ── renew ────────────────────────────────────────────────────────────────

    async def tick(self) -> bool:
        """One turn of §3 requirement 5's five-minute cadence.

        Its purpose is not the access token, which any call would refresh on demand.
        **Every refresh resets the refresh token's one-hour life**, and that hour is
        the entire margin between a restart that survives unattended and one that needs
        a human at a browser — so this runs whether or not anything is reading a chart,
        including all weekend while the market is shut.

        Never raises. It is a scheduler job, and a job that threw would stop being run
        on some configurations — the credential keeper going quiet is the one outcome
        worse than the credential expiring.
        """
        fingerprint: str | None = None
        try:
            async with self._auth_factory(self._settings, self._database) as auth:
                # Read before the attempt, from the auth's OWN store. After a failed
                # refresh the credential may be gone, and the alert needs to name the
                # one that died rather than the absence it left behind.
                fingerprint = await auth.stored_fingerprint()
                bundle = await auth.ensure_fresh()
        except ReauthenticationRequired as exc:
            # The chain is dead and no amount of retrying revives it: the refresh token
            # is single-use and it is gone. This is the branch the whole module exists
            # for, and it is the one that must not be silent.
            await self._tell(str(exc), fingerprint=fingerprint)
            return False
        except TokenPersistFailed as exc:
            # A 2xx that did not survive the write. The credential was spent and thrown
            # away, so the chain is dead in a different way with the same remedy —
            # spike defect C1, arriving from the direction §3 requirement 2 predicted.
            log.error("forex.token_persist_failed", error=str(exc))
            await self._tell(str(exc), fingerprint=fingerprint)
            return False
        except Exception as exc:  # pragma: no cover — defence in depth
            # A vendor 5xx or a timeout is not a dead credential: forex degrades for
            # this tick and the next one tries again. No alert, because the owner has
            # nothing to do about it.
            log.warning("forex.token_refresh_failed", error=str(exc), error_type=type(exc).__name__)
            return False
        if bundle is not None:
            log.info("forex.token_refresh_tick", refresh_count=bundle.refresh_count)
        return True

    # ── tell ─────────────────────────────────────────────────────────────────

    async def _tell(self, detail: str, *, fingerprint: str | None = None) -> None:
        """The alert §3.1 asks for: the action named, the URL attached.

        "Forex paused" would send the owner hunting a data problem. What he has is a
        two-minute browser login, and the message says so and carries the address.
        """
        owner_id = self._settings.secrets.owner_user_id
        log.error(
            "forex.reauth_required",
            detail=detail,
            credential=fingerprint or "none-stored",
        )
        if self._notices is None or owner_id is None:
            return
        alert = reauth_alert(
            authorize_url=self._settings.config.forex.authorize_url,
            detail=detail,
            since=self._clock.now(),
        )
        await self._notices.notice(
            owner_id,
            key=reauth_key(fingerprint),
            text=alert_card(
                alert_view(alert), zone_info(self._settings.config.telegram.owner_timezone)
            ),
        )


__all__ = ["AuthFactory", "ForexCredentialKeeper", "SupportsNotice", "reauth_key"]
