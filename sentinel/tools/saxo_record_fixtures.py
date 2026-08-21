"""Replace the reconstructed Saxo fixtures with recorded ones — run by hand (M10b).

    python -m sentinel.tools.saxo_record_fixtures --login    # log in, then verify
    python -m sentinel.tools.saxo_record_fixtures --check    # verify, write nothing
    python -m sentinel.tools.saxo_record_fixtures            # re-record the fixtures

``--login`` does the browser login **and the verification in one process**, and that
is not a convenience. The refresh token is single-use and rotates, so a login in one
invocation and a check in the next would spend the credential in between and the second
command would fail with an expired chain (specs/FOREX.md §3).

**Why this exists (owner requirement R-e, 2026-08-21).** The spike's 89 raw JSON
evidence files were deleted along with its throwaway scripts (commit 4c90819).
Dropping the scripts was right; the raw responses were evidence and should have been
kept. What survives is ``journal/M10b_SPIKE.md``, so every Saxo fixture in the test
suite is hand-authored **from the same document the code was written from** — and if
the spike misread the API anywhere, the fixture reproduces the misreading and the
test passes against a wrong world. That is how the pip bug nearly survived: a
plausible value agreeing with a plausible assumption.

So this script exists to close the loop against reality, whenever the owner has a
few minutes and a valid credential. It is **never** part of ``make check``: it needs
network and credentials, and CLAUDE.md forbids live-API tests in the suite.

**Read-only, structurally.** It reaches ``/ref`` and ``/chart`` and nothing else. No
order endpoint is imported or reachable, and the app registration itself was created
with the trading checkbox unchecked.

``--check`` additionally runs the **tail-consistency check** the owner asked for
(correction C3). The forex 1h tail asks for 1200 bars rather than 321 so the
hour-of-day spread profile rests on ~35 samples per bucket instead of ~10. That is
only safe if a 1200-bar read and a 321-bar read agree about the bars they share —
and spike defect D-d found the chart series is *not* stable across queries, with the
same hour present or absent depending on anchor and ``Count``. This is the check that
settles it. **Until it has been run, that agreement is an expectation and not a
finding**, and journal/M10b_REPORT.md says so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets as token_source
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from pydantic import SecretStr

from sentinel.core.clock import SystemClock
from sentinel.core.config import Settings, load_settings
from sentinel.features.engine import compute_timeframe
from sentinel.fx.models import SaxoTokenBundle
from sentinel.fx.tails import compare_tail_overlap
from sentinel.ingestion.adapters.forex_saxo import ASSET_TYPE, ForexTail, SaxoForexAdapter
from sentinel.ingestion.clients.saxo_auth import SOURCE as AUTH_SOURCE
from sentinel.ingestion.clients.saxo_auth import SaxoAuth
from sentinel.ingestion.http import HttpFetcher

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes"
SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY")
#: The two reads C3 compares. 321 is what the feature engine consumes; 1200 is the
#: whole tail the spread profile is built from.
FEATURE_COUNT = 321
PROFILE_COUNT = 1200


def _provenance(source: str, at: datetime) -> dict[str, Any]:
    return {
        "status": "RECORDED",
        "note": (
            "RECORDED from the live Saxo API by "
            "python -m sentinel.tools.saxo_record_fixtures. This replaced a "
            "RECONSTRUCTED fixture hand-authored from journal/M10b_SPIKE.md."
        ),
        "source": source,
        "recorded_at": at.isoformat(),
        "values_from_spike": {"superseded": "this file is a capture, not a reconstruction"},
    }


def _write(name: str, payload: dict[str, Any]) -> None:
    path = CASSETTE_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path.name} ({path.stat().st_size:,} bytes)")


def _settings() -> Settings:
    settings = load_settings()
    missing = [
        name
        for name, value in (
            ("SAXO_APP_KEY", settings.secrets.saxo_app_key),
            ("SAXO_APP_SECRET", settings.secrets.saxo_app_secret),
        )
        if value is None or not value.get_secret_value().strip()
    ]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} in .env")
    return settings


def _fetcher(settings: Settings) -> tuple[HttpFetcher, httpx.AsyncClient]:
    client = httpx.AsyncClient(follow_redirects=True)
    return (
        HttpFetcher(
            client,
            timeout_seconds=settings.config.ingestion.request_timeout_seconds,
            max_retries=settings.config.ingestion.max_retries,
            backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
        ),
        client,
    )


class _MemoryStore:
    """A store that keeps the rotated credential for this process only.

    Deliberately **not** the Postgres one: running this on a laptop must never consume
    and discard the server's live single-use refresh token.
    """

    def __init__(self, bundle: SaxoTokenBundle) -> None:
        self.bundle = bundle

    async def load(self) -> SaxoTokenBundle | None:
        return self.bundle

    async def save(self, bundle: SaxoTokenBundle) -> None:
        self.bundle = bundle


def _adapter(
    settings: Settings, fetcher: HttpFetcher, bundle: SaxoTokenBundle
) -> tuple[SaxoForexAdapter, SaxoAuth]:
    auth = SaxoAuth(
        settings.config.forex,
        fetcher=fetcher,
        store=_MemoryStore(bundle),
        app_key=_secret(settings.secrets.saxo_app_key),
        app_secret=_secret(settings.secrets.saxo_app_secret),
    )
    adapter = SaxoForexAdapter(
        settings.config.forex, fetcher=fetcher, tokens=auth, clock=SystemClock()
    )
    return adapter, auth


def _secret(value: SecretStr | None) -> SecretStr:
    assert value is not None  # checked in _settings
    return value


# ── the browser login (§3.1) ────────────────────────────────────────────────


def authorize_url(settings: Settings, state: str) -> str:
    """The URL to open. The code lands on a localhost callback with no listener.

    That is deliberate and it is why this is a copy-paste flow rather than a server:
    the deployment's **zero inbound ports** property is worth more than the
    convenience, so the browser shows an error page and the address bar carries the
    code (specs/FOREX.md §3.1).
    """
    forex = settings.config.forex
    redirect = settings.secrets.saxo_redirect_uri
    return (
        f"{forex.authorize_url}"
        f"?response_type=code"
        f"&client_id={quote(_secret(settings.secrets.saxo_app_key).get_secret_value(), safe='')}"
        f"&redirect_uri={quote(redirect, safe='')}"
        f"&state={quote(state, safe='')}"
    )


def code_from(pasted: str, *, expected_state: str) -> str:
    """Pull ``code`` out of the pasted redirect URL, verifying ``state`` first.

    The state check is not ceremony: it is what makes the code that arrives the code
    this process asked for, and skipping it in a "just a local script" is how a habit
    of skipping it starts.
    """
    text = pasted.strip()
    query = parse_qs(urlparse(text).query) if "?" in text else parse_qs(text.lstrip("?"))
    if "error" in query:
        raise SystemExit(f"the redirect carries an error: {query['error'][0]}")
    state = query.get("state", [""])[0]
    if state != expected_state:
        raise SystemExit(
            "the redirect's state does not match the one this process generated — "
            "paste the URL from the browser window this command opened, not an older one"
        )
    code = query.get("code", [""])[0]
    if not code:
        raise SystemExit(f"no 'code' parameter in {text[:80]!r}")
    return code


async def exchange_code(settings: Settings, fetcher: HttpFetcher, code: str) -> SaxoTokenBundle:
    """Authorization code -> the first token pair.

    Accepts any 2xx: Saxo answers this one **201**, and the spike's first version
    checked ``!= 200`` and threw a live credential away down the error branch.
    Every lifetime is read from the response, never assumed.
    """
    now = datetime.now(UTC)
    payload = await fetcher.post_form(
        settings.config.forex.token_url,
        source=AUTH_SOURCE,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.secrets.saxo_redirect_uri,
        },
        auth=(
            _secret(settings.secrets.saxo_app_key).get_secret_value(),
            _secret(settings.secrets.saxo_app_secret).get_secret_value(),
        ),
    )
    if not isinstance(payload, dict):
        raise SystemExit("the token endpoint did not return a JSON object")
    for field in ("access_token", "refresh_token", "expires_in"):
        if field not in payload:
            raise SystemExit(f"the token response has no {field}")
    print(
        f"  exchanged OK · expires_in={payload['expires_in']} "
        f"refresh_token_expires_in={payload.get('refresh_token_expires_in', 'absent')}"
    )
    refresh_expires_in = payload.get("refresh_token_expires_in")
    return SaxoTokenBundle(
        access_token=SecretStr(str(payload["access_token"])),
        refresh_token=SecretStr(str(payload["refresh_token"])),
        obtained_at=now,
        access_expires_at=now + timedelta(seconds=int(payload["expires_in"])),
        refresh_expires_at=(
            now + timedelta(seconds=int(refresh_expires_in))
            if isinstance(refresh_expires_in, int)
            else None
        ),
    )


# ── the three questions this must settle ────────────────────────────────────


async def run_checks(settings: Settings, adapter: SaxoForexAdapter) -> int:
    """C3 (1) and (2), plus the fixture-vs-reality comparison. Writes nothing."""
    failures: list[str] = []
    print("\n" + "=" * 72)
    print("1. does a 1200-bar request return exactly 1200?")
    print("=" * 72)
    tails: dict[str, ForexTail] = {}
    for symbol in SYMBOLS:
        try:
            long_tail = await adapter.fetch_tail(symbol, "1h", PROFILE_COUNT)
        except Exception as exc:
            failures.append(f"{symbol}: 1200-bar read failed — {exc}")
            print(f"  {symbol}: FAIL — {exc}")
            continue
        tails[symbol] = long_tail
        print(
            f"  {symbol}: asked {PROFILE_COUNT}, the count assertion passed, "
            f"{len(long_tail.bid.candles)} closed after dropping the forming bar "
            f"(oldest {long_tail.bid.candles[0].open_time.isoformat()})"
        )

    print("\n" + "=" * 72)
    print("2. are features from the last 321 of the 1200 identical to a direct 321 read?")
    print("=" * 72)
    now = datetime.now(UTC)
    for symbol in SYMBOLS:
        profile_tail = tails.get(symbol)
        if profile_tail is None:
            continue
        short_tail = await adapter.fetch_tail(symbol, "1h", FEATURE_COUNT)

        newest_long = profile_tail.bid.candles[-1].open_time
        newest_short = short_tail.bid.candles[-1].open_time
        if newest_long != newest_short:
            print(
                f"  {symbol}: a bar rolled between the two reads "
                f"({newest_long.isoformat()} vs {newest_short.isoformat()}). "
                f"Not an anomaly — re-run to compare a stable window."
            )
            failures.append(f"{symbol}: inconclusive, a bar rolled mid-check")
            continue

        candle_diffs = compare_tail_overlap(profile_tail.bid, short_tail.bid)
        tail_of_long = profile_tail.bid.model_copy(
            update={"candles": profile_tail.bid.candles[-len(short_tail.bid.candles) :]}
        )
        features_config = settings.config.features
        from_long = compute_timeframe(tail_of_long, features_config, now=now)
        from_short = compute_timeframe(short_tail.bid, features_config, now=now)
        same_features = (
            from_long is not None
            and from_short is not None
            and from_long.model_dump(mode="json") == from_short.model_dump(mode="json")
        )

        if not candle_diffs and same_features:
            print(f"  {symbol}: PASS — every shared bar agrees, and the features are identical")
            continue

        failures.append(f"{symbol}: the two reads disagree")
        print(f"  {symbol}: **FAIL** — {len(candle_diffs)} candle disagreement(s)")
        for diff in candle_diffs[:10]:
            print(f"      {diff}")
        if not same_features and from_long is not None and from_short is not None:
            long_dump, short_dump = (
                from_long.model_dump(mode="json"),
                from_short.model_dump(mode="json"),
            )
            for key in sorted(long_dump):
                if long_dump[key] != short_dump[key]:
                    print(
                        f"      feature {key}: "
                        f"from-1200={long_dump[key]} from-321={short_dump[key]}"
                    )

    print("\n" + "=" * 72)
    print("3. do the reconstructed fixtures match live reality?")
    print("=" * 72)
    failures.extend(await compare_fixtures(adapter, tails))

    print("\n" + "=" * 72)
    if failures:
        print("RESULT: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nIf the failures are in (2), STOP. That is spike defect D-d's anchor "
            "instability at a new boundary, and the spread baseline rests on it."
        )
        return 1
    print("RESULT: PASS — all three settled")
    return 0


async def compare_fixtures(adapter: SaxoForexAdapter, tails: dict[str, ForexTail]) -> list[str]:
    """Every value the committed fixtures assert, checked against the live API.

    Reported rather than overwritten. Silently re-recording over a mismatch would
    destroy the only evidence that the reconstruction was wrong.
    """
    mismatches: list[str] = []
    details_fixture = json.loads((CASSETTE_DIR / "saxo_ref_details.json").read_text("utf-8"))
    by_symbol = {entry["Symbol"]: entry for entry in details_fixture["Data"]}

    for symbol in SYMBOLS:
        fixture = by_symbol[symbol]
        search = json.loads(
            (CASSETTE_DIR / f"saxo_ref_instruments_{symbol}.json").read_text("utf-8")
        )
        fixture_uic = next(e["Identifier"] for e in search["Data"] if e["Symbol"] == symbol)
        try:
            live = await adapter.resolve(symbol)
        except Exception as exc:
            mismatches.append(f"{symbol}: could not resolve live — {exc}")
            print(f"  {symbol}: FAIL — {exc}")
            continue

        checks = (
            ("Uic", fixture_uic, live.uic),
            ("Format.Decimals", fixture["Format"]["Decimals"], live.decimals),
            ("TickSize", Decimal(str(fixture["TickSize"])), live.tick_size),
            ("MinimumTradeSize", Decimal(str(fixture["MinimumTradeSize"])), live.min_trade_size),
            ("AmountDecimals", fixture["AmountDecimals"], live.amount_decimals),
        )
        bad = [(name, was, now_) for name, was, now_ in checks if was != now_]
        if bad:
            for name, was, now_ in bad:
                mismatches.append(f"{symbol}.{name}: fixture {was}, live {now_}")
                print(f"  {symbol}: **MISMATCH** {name} — fixture {was}, live {now_}")
        else:
            print(
                f"  {symbol}: matches — Uic {live.uic}, Decimals {live.decimals}, "
                f"pip {live.pip}, TickSize {live.tick_size}, "
                f"MinimumTradeSize {live.min_trade_size}"
            )

    chart_fixture = json.loads((CASSETTE_DIR / "saxo_chart_EURUSD_60.json").read_text("utf-8"))
    fixture_keys = sorted(chart_fixture["Data"][0])
    live_payload = await adapter._get(
        "/chart/v3/charts",
        params={
            "AssetType": ASSET_TYPE,
            "Uic": str((await adapter.resolve("EURUSD")).uic),
            "Horizon": "60",
            "Count": "10",
        },
    )
    live_keys = sorted(live_payload["Data"][0])
    if live_keys == fixture_keys:
        print(f"  chart row keys match: {live_keys}")
    else:
        mismatches.append(f"chart row keys: fixture {fixture_keys}, live {live_keys}")
        print(
            f"  **MISMATCH** chart row keys\n"
            f"      fixture {fixture_keys}\n"
            f"      live    {live_keys}"
        )
    for name in ("ChartInfo", "DisplayAndFormat"):
        value = live_payload.get(name)
        print(f"  {name}: {value!r} (D-h expects an empty object)")
    return mismatches


async def login_and_check() -> int:
    settings = _settings()
    state = token_source.token_urlsafe(24)
    print("\n" + "=" * 72)
    print("Open this URL in a browser, log in, and approve.")
    print("The page will fail to load — that is expected and correct: the callback is")
    print("localhost and nothing is listening there. Copy the URL from the ADDRESS BAR.")
    print("=" * 72 + "\n")
    print(authorize_url(settings, state))
    print()
    # Off the event loop: `input()` blocks, and blocking an event loop that is about
    # to make the one time-sensitive call in this whole flow is a poor habit even when
    # nothing else is running on it.
    pasted = await asyncio.to_thread(
        input, "Paste the full redirect URL here, then press Enter:\n> "
    )

    fetcher, client = _fetcher(settings)
    async with client:
        bundle = await exchange_code(settings, fetcher, code_from(pasted, expected_state=state))
        adapter, _ = _adapter(settings, fetcher, bundle)
        return await run_checks(settings, adapter)


async def check_with_stored_token() -> int:
    settings = _settings()
    refresh = settings.secrets.saxo_refresh_token
    if refresh is None or not refresh.get_secret_value().strip():
        raise SystemExit(
            "no SAXO_REFRESH_TOKEN in .env — run with --login instead, which does the "
            "browser login and the checks in one process (the token is single-use)"
        )
    fetcher, client = _fetcher(settings)
    async with client:
        adapter, _ = _adapter(
            settings, fetcher, SaxoTokenBundle(refresh_token=refresh, obtained_at=datetime.now(UTC))
        )
        return await run_checks(settings, adapter)


async def record() -> None:
    settings = _settings()
    refresh = settings.secrets.saxo_refresh_token
    if refresh is None:
        raise SystemExit("no SAXO_REFRESH_TOKEN in .env — obtain one with --login first")
    fetcher, client = _fetcher(settings)
    at = datetime.now(UTC)
    async with client:
        adapter, _ = _adapter(
            settings, fetcher, SaxoTokenBundle(refresh_token=refresh, obtained_at=at)
        )
        for symbol in SYMBOLS:
            print(f"{symbol}")
            search = await adapter._get(
                "/ref/v1/instruments", params={"Keywords": symbol, "AssetTypes": ASSET_TYPE}
            )
            _write(
                f"saxo_ref_instruments_{symbol}.json",
                {"_provenance": _provenance("/ref/v1/instruments", at), **search},
            )
        uics = [str((await adapter.resolve(symbol)).uic) for symbol in SYMBOLS]
        details = await adapter._get(
            "/ref/v1/instruments/details",
            params={"Uics": ",".join(uics), "AssetTypes": ASSET_TYPE},
        )
        _write(
            "saxo_ref_details.json",
            {"_provenance": _provenance("/ref/v1/instruments/details", at), **details},
        )
        chart = await adapter._get(
            "/chart/v3/charts",
            params={
                "AssetType": ASSET_TYPE,
                "Uic": uics[0],
                "Horizon": "60",
                "Count": "10",
            },
        )
        _write(
            "saxo_chart_EURUSD_60.json",
            {"_provenance": _provenance("/chart/v3/charts Horizon=60 Count=10", at), **chart},
        )
    print("\nfixtures are now RECORDED. Re-run the suite and update journal/M10b_REPORT.md §8.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Saxo fixture recorder and live checker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--login",
        action="store_true",
        help="browser login, then the checks, in ONE process (the token is single-use)",
    )
    group.add_argument(
        "--check", action="store_true", help="run the checks with the stored token; write nothing"
    )
    args = parser.parse_args()

    if args.login:
        raise SystemExit(asyncio.run(login_and_check()))
    if args.check:
        raise SystemExit(asyncio.run(check_with_stored_token()))
    asyncio.run(record())


if __name__ == "__main__":
    main()
