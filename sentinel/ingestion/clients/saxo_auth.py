"""The Saxo OAuth chain, and why it needs this much care (FOREX.md §3).

FOREX.md v1 said, in effect, "log in once, forever". The live spike found that to be
false in a way that is operational rather than cosmetic:

===========================  =========================================
access token lifetime        ~20 min, and it **varies** (1070/1182/1200 s observed)
refresh token lifetime       ~1 hour (3582/3600 s observed)
refresh token on use         **rotates — single use**
token exchange status        **201**, not 200
unattended credential        **none at this account tier**
===========================  =========================================

Together those give the credential chain a **one-hour memory**. Routine operation is
fine; a deploy or restart inside the hour survives *if* the rotated token was
persisted; any outage longer than an hour kills the chain and needs a human at a
browser. Certificate Based Authentication is the only unattended option Saxo
documents and it is partners-only, so this is a standing cost of the provider and
not a bug to engineer away.

Five rules follow, and every one of them is here because ignoring it produces a
system that looks healthy until it isn't:

1. **Persist on every refresh.** A persist-once design works through exactly one
   refresh and then dies at whatever hour the container happens to restart.
2. **Verify the write by reading it back.** A 2xx is not proof of a save. This
   happened during the spike: a successful exchange printed success and persisted
   nothing, so the single-use credential was issued and thrown away.
3. **Accept any 2xx.** The spike's first version checked ``!= 200`` and sent a
   perfectly good ``201 Created`` down the error branch.
4. **Read every lifetime from the response.** They are not round numbers and they
   differ between responses; a constant is wrong by construction.
5. **Fail loudly and specifically.** "Forex paused" and "you need to log in again"
   are different messages with different actions, and only one of them is a
   two-minute fix.

Nothing here ever puts a token value in a log line. Fingerprints only — the same
sha256-prefix discipline the spike used to compare two runs without exposing either.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import SecretStr

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import ForexConfig
from sentinel.core.logging import get_logger
from sentinel.fx.errors import ReauthenticationRequired, TokenPersistFailed
from sentinel.fx.models import SaxoTokenBundle
from sentinel.ingestion.errors import SourceUnavailable

log = get_logger(__name__)

SOURCE = "saxo_oauth"


class TokenStore(Protocol):
    """Durable storage for the rotating credential.

    A seam, so the manager's rules are testable without a database — and so the
    read-back check below is a real round trip through whatever storage is in use
    rather than a comparison with a value still in hand.
    """

    async def load(self) -> SaxoTokenBundle | None: ...

    async def save(self, bundle: SaxoTokenBundle) -> None: ...


class SaxoAuth:
    """Keeps a usable access token, and says clearly when it cannot."""

    def __init__(
        self,
        config: ForexConfig,
        *,
        fetcher: Any,
        store: TokenStore,
        app_key: SecretStr,
        app_secret: SecretStr,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._store = store
        #: Exposed so a caller that already built this chain can reuse its HTTP client
        #: for the chart adapter rather than opening a second one (M10d,
        #: :func:`sentinel.core.wiring.forex_adapter`). Read-only by convention: the
        #: fetcher is the caller's and closing it is the caller's job.
        self.fetcher = fetcher
        self._app_key = app_key
        self._app_secret = app_secret
        self._clock = clock or SystemClock()

    # ── bootstrap ────────────────────────────────────────────────────────────

    async def bootstrap(self, refresh_token: SecretStr) -> SaxoTokenBundle:
        """Seed an **empty** store from the manual browser login's refresh token.

        Empty, and only empty. The stored credential always wins, because the token
        in ``.env`` is a single-use one that was spent the first time the service
        refreshed: letting a redeploy overwrite the live chain with it would end the
        chain and require the very login this is trying to avoid. That is the whole
        of §3 requirement 1 arriving from an unexpected direction, so it has its own
        test.

        The seeded bundle has **no known refresh expiry**. Nobody told us one, and
        inventing an hour would be assuming an operational value — the mistake spike
        defect D-c is about. Unknown stays unknown until the first refresh answers it.
        """
        existing = await self._store.load()
        if existing is not None:
            log.info(
                "forex.auth_bootstrap_skipped",
                detail="a stored credential already exists and it is the live one",
                stored=existing.refresh_fingerprint,
                offered=SaxoTokenBundle(
                    refresh_token=refresh_token, obtained_at=self._clock.now()
                ).refresh_fingerprint,
            )
            return existing

        seeded = SaxoTokenBundle(refresh_token=refresh_token, obtained_at=self._clock.now())
        await self._persist(seeded)
        log.info("forex.auth_bootstrapped", refresh=seeded.refresh_fingerprint)
        return seeded

    async def stored_fingerprint(self) -> str | None:
        """The stored credential's fingerprint, or ``None`` when nothing is stored.

        A fingerprint, never a value — the same sha256-prefix discipline the spike used
        to compare two runs without exposing either. It exists so the re-authentication
        alert can be keyed on **which** credential died (M10d): one message per dead
        chain, and a fresh one after a fresh login, where a date-keyed alert would
        suppress the second death of the day.

        On the auth object rather than on the caller, so there is one store here and
        not two — a caller that built its own would be reading a different object that
        merely happens to point at the same table.
        """
        current = await self._store.load()
        return None if current is None else current.refresh_fingerprint

    # ── the token a caller actually wants ────────────────────────────────────

    async def access_token(self) -> str:
        """A bearer token that is valid now and will still be valid on arrival."""
        now = self._clock.now()
        current = await self._store.load()
        margin = timedelta(seconds=self._config.token_expiry_margin_seconds)
        if current is not None and current.access_is_usable(now, margin=margin):
            token = current.access_token_value
            assert token is not None  # access_is_usable checked it
            return token
        refreshed = await self.refresh()
        token = refreshed.access_token_value
        if token is None:  # pragma: no cover — a 2xx without a token already raised
            raise ReauthenticationRequired("the refresh returned no access token")
        return token

    async def ensure_fresh(self) -> SaxoTokenBundle | None:
        """The five-minute cadence job (§3 requirement 5).

        Its purpose is **not** really the access token, which a call would refresh on
        demand anyway. Every refresh resets the refresh token's one-hour life, and
        that hour is the entire margin between a restart that survives unattended and
        one that needs a human at a browser. So this runs whether or not anybody is
        asking for a token — including all weekend, when the market is shut and no
        chart is being read.

        Returns ``None`` when nothing needed doing.
        """
        now = self._clock.now()
        current = await self._store.load()
        if current is None:
            raise ReauthenticationRequired(
                "no Saxo credential is stored — a manual browser login is needed to seed one"
            )
        due_at = current.obtained_at + timedelta(
            seconds=self._config.token_refresh_interval_seconds
        )
        if now < due_at:
            return None
        return await self.refresh()

    # ── the refresh itself ───────────────────────────────────────────────────

    async def refresh(self) -> SaxoTokenBundle:
        """Exchange the stored refresh token for a new pair, and prove it was saved."""
        now = self._clock.now()
        current = await self._store.load()
        if current is None:
            raise ReauthenticationRequired(
                "no Saxo credential is stored — a manual browser login is needed to seed one"
            )
        if current.refresh_is_dead(now):
            raise ReauthenticationRequired(
                f"the refresh token expired at {current.refresh_expires_at} — the chain "
                f"has a one-hour memory and this outage was longer than that"
            )

        try:
            payload = await self._fetcher.post_form(
                self._config.token_url,
                source=SOURCE,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token_value,
                    "redirect_uri": self._config.authorize_url,
                },
                auth=(self._app_key.get_secret_value(), self._app_secret.get_secret_value()),
            )
        except SourceUnavailable as exc:
            # A 4xx on a refresh means the credential itself is the problem — spent,
            # revoked, or rotated out from under us — and no amount of retrying fixes
            # that. A 5xx or a timeout is the vendor, and forex pauses and retries.
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                raise ReauthenticationRequired(
                    f"Saxo refused the refresh token ({exc.reason}) — it is single-use "
                    f"and cannot be recovered without a browser login"
                ) from exc
            raise

        bundle = _bundle_from(payload, now=now, previous=current)
        await self._persist(bundle)
        log.info(
            "forex.auth_refreshed",
            refresh_count=bundle.refresh_count,
            # Fingerprints, never values. Rotation is visible; the credential is not.
            refresh_before=current.refresh_fingerprint,
            refresh_after=bundle.refresh_fingerprint,
            refresh_rotated=current.refresh_fingerprint != bundle.refresh_fingerprint,
            access_expires_at=None
            if bundle.access_expires_at is None
            else bundle.access_expires_at.isoformat(),
            refresh_expires_at=None
            if bundle.refresh_expires_at is None
            else bundle.refresh_expires_at.isoformat(),
        )
        return bundle

    async def _persist(self, bundle: SaxoTokenBundle) -> None:
        """Save, then **read back and compare** (§3 requirement 2).

        The inference "the POST succeeded, therefore the token is safe" is exactly
        what threw a live credential away during the spike. What is checked is the
        round trip: the tokens that come back out, and the expiries stored with them.
        """
        await self._store.save(bundle)
        stored = await self._store.load()
        if stored is None:
            raise TokenPersistFailed(
                "the refreshed Saxo token did not survive the write — the store read "
                "back empty, and the token that was just spent cannot be re-used"
            )
        mismatches = [
            name
            for name, saved, read in (
                ("refresh_token", bundle.refresh_fingerprint, stored.refresh_fingerprint),
                ("access_token", bundle.access_fingerprint, stored.access_fingerprint),
                ("access_expires_at", bundle.access_expires_at, stored.access_expires_at),
                ("refresh_expires_at", bundle.refresh_expires_at, stored.refresh_expires_at),
            )
            if saved != read
        ]
        if mismatches:
            raise TokenPersistFailed(
                f"the refreshed Saxo token did not round-trip: {', '.join(mismatches)} "
                f"read back differently from what was written"
            )


def _bundle_from(payload: Any, *, now: datetime, previous: SaxoTokenBundle) -> SaxoTokenBundle:
    """Build the new credential from the response, reading every value it carries.

    ``access_token``, ``refresh_token`` and ``expires_in`` are required: a response
    missing any of them is not a successful refresh whatever its status code said.

    ``refresh_token_expires_in`` is treated as optional and its absence means
    **unknown**, not "does not expire". The spike saw it on every response it could
    read, but one earlier response had it stripped by a scrubber — and a missing
    lifetime that silently became "no expiry" would turn a dead chain into one that
    looks alive right up until every request fails at once.
    """
    if not isinstance(payload, dict):
        raise SourceUnavailable(SOURCE, "token response was not a JSON object")

    access = _require_str(payload, "access_token")
    refresh = _require_str(payload, "refresh_token")
    expires_in = _require_int(payload, "expires_in")
    refresh_expires_in = payload.get("refresh_token_expires_in")

    refresh_expires_at: datetime | None = None
    if isinstance(refresh_expires_in, int) and not isinstance(refresh_expires_in, bool):
        refresh_expires_at = now + timedelta(seconds=refresh_expires_in)

    return SaxoTokenBundle(
        refresh_token=SecretStr(refresh),
        access_token=SecretStr(access),
        obtained_at=now,
        access_expires_at=now + timedelta(seconds=expires_in),
        refresh_expires_at=refresh_expires_at,
        refresh_count=previous.refresh_count + 1,
    )


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SourceUnavailable(SOURCE, f"token response has no {key}")
    return value


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceUnavailable(SOURCE, f"token response has no integer {key}")
    return value


__all__ = ["SOURCE", "SaxoAuth", "TokenStore"]
