"""Configuration: secrets from the environment, everything else from ``config.yaml``.

Two layers, deliberately separate:

* :class:`Secrets` — env / ``.env`` only. Never logged, never written to yaml.
* :class:`AppConfig` — non-secret runtime knobs, defaults defined here and
  overridable by ``config.yaml`` and (later) by DB values set via Telegram.

Precedence per ARCHITECTURE.md §2: **DB > yaml > defaults**. ``db_overrides`` is
wired and tested now; the DB-backed source arrives with the settings table.

Percentages that feed sizing math are ``Decimal`` — money math is never float
(CLAUDE.md).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentinel.core.markets import LEGACY_MARKET, Market

DEFAULT_CONFIG_PATH = Path("config.yaml")


def _as_decimal(value: Any) -> Any:
    """Convert YAML/JSON numbers to ``Decimal`` via ``str`` (no float artefacts)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    return value


Dec = Annotated[Decimal, BeforeValidator(_as_decimal)]


class _Strict(BaseModel):
    """Base for config models: unknown keys are an error, values immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


class Secrets(BaseSettings):
    """Secrets and deployment-specific values. Sourced from env / ``.env`` only.

    Secret fields are :class:`~pydantic.SecretStr`, so they cannot leak through a
    repr, a log line, or an exception rendering.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    sentinel_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    health_host: str = "0.0.0.0"  # container-local bind; Compose controls exposure
    health_port: int = 8000
    config_path: Path = DEFAULT_CONFIG_PATH

    database_url: str = "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"

    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    #: Who owns this deployment (M8.1). The ``users`` table is the runtime authority
    #: on standing and roles; this names the row to seed and is the one identity that
    #: cannot be granted from inside Telegram.
    telegram_owner_user_id: int | None = None
    # Comma-separated in the environment; parsed by `allowed_user_ids`.
    telegram_allowed_user_ids: str = ""
    cryptopanic_api_key: SecretStr | None = None

    # ── Saxo Bank OpenAPI (M10b, specs/FOREX.md §3) ──────────────────────────
    #
    # Read-only market data. The app registration was created with the trading
    # checkbox unchecked, so this credential cannot place an order even if something
    # tried to — the same structural guarantee the keyless ccxt client gives crypto.
    #
    # ``saxo_refresh_token`` is a BOOTSTRAP value only. It seeds an empty token store
    # after a manual browser login and is never written back to: the refresh token
    # rotates on every use and lives in Postgres from then on. A redeploy that let
    # this variable win over the stored value would rewind the chain to a single-use
    # token that has already been spent (specs/FOREX.md §3 requirement 1).
    saxo_app_key: SecretStr | None = None
    saxo_app_secret: SecretStr | None = None
    saxo_refresh_token: SecretStr | None = None
    #: Where the authorization code lands during a manual login. ``localhost`` on the
    #: owner's own Mac, deliberately: it keeps the deployment's zero-inbound-ports
    #: property intact (§3.1).
    saxo_redirect_uri: str = "https://localhost:8080/callback"

    @property
    def allowed_user_ids(self) -> tuple[int, ...]:
        """The pre-M8.1 allowlist. **Bootstrap only** from M8.1 onwards.

        Through M8 this tuple was both the authorization list and the broadcast
        list. Both jobs moved to the ``users`` table, which a person can be *added*
        to without a deploy. What survives here is its use as a fallback for
        :attr:`owner_user_id`, so an existing single-id ``.env`` keeps working
        untouched.
        """
        raw = self.telegram_allowed_user_ids.strip()
        if not raw:
            return ()
        return tuple(int(part.strip()) for part in raw.split(",") if part.strip())

    @property
    def owner_user_id(self) -> int | None:
        """The owner's Telegram id, or ``None`` if the environment names none.

        ``TELEGRAM_ALLOWED_USER_IDS`` is only trusted when it holds a single id: a
        multi-id allowlist predates roles entirely and says nothing about which of
        those ids owns the system. Guessing would hand approval rights to whoever
        happened to be listed first, so the fallback declines instead and migration
        0007 fails with the variable's name in the message.
        """
        if self.telegram_owner_user_id is not None:
            return self.telegram_owner_user_id
        allowed = self.allowed_user_ids
        return allowed[0] if len(allowed) == 1 else None

    @property
    def json_logs(self) -> bool:
        return self.sentinel_env != "dev"


# --------------------------------------------------------------------------- #
# App config (config.yaml)
# --------------------------------------------------------------------------- #


class ScheduleConfig(_Strict):
    scan_interval_minutes: int = 60
    #: M8.2 — how long a symbol stays quiet after a deep analysis that was not a
    #: candidate. One setup-timeframe candle: the analyst reads a 1h chart, so
    #: re-asking sooner buys the same verdict about the same unclosed bar. 0 disables.
    reanalysis_cooldown_minutes: int = Field(default=60, ge=0)
    tracker_interval_seconds: int = 60
    heartbeat_interval_seconds: int = 60
    symbol_timeout_seconds: int = 20


class TimeframeSpec(_Strict):
    timeframe: str
    candles: int


class MarketDataConfig(_Strict):
    exchange: str = "binance_usdm"
    orderbook_depth: int = 50
    #: Always one more than the closed-candle requirement: the last candle is
    #: still in progress and gets dropped. Chart timeframes carry 320 closed so
    #: EMA200 has enough valid points to span the 120-candle chart window (M3);
    #: 1d keeps DATA_SOURCES §2.1's 100.
    timeframes: tuple[TimeframeSpec, ...] = (
        TimeframeSpec(timeframe="15m", candles=321),
        TimeframeSpec(timeframe="1h", candles=321),
        TimeframeSpec(timeframe="4h", candles=321),
        TimeframeSpec(timeframe="1d", candles=101),
    )


class MaxAgeConfig(_Strict):
    """specs/DATA_SOURCES.md §4 — staleness budgets, in seconds."""

    ohlcv_multiplier: int = 2
    funding: int = 900
    open_interest: int = 1800
    long_short_ratio: int = 1800
    orderbook: int = 300
    fear_greed: int = 86_400
    btc_dominance: int = 86_400
    news_window: int = 21_600
    eurusd: int = 3_600


class DataQualityConfig(_Strict):
    max_age_seconds: MaxAgeConfig = MaxAgeConfig()


class NewsConfig(_Strict):
    """specs/DATA_SOURCES.md §2.2. The CryptoPanic key itself lives in .env."""

    limit: int = 20
    important_only: bool = True
    cryptopanic_base_url: str = "https://cryptopanic.com/api/v1/posts/"
    rss_feeds: tuple[str, ...] = (
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    )


class IngestionConfig(_Strict):
    """specs/DATA_SOURCES.md §2-§3 — timeouts, retries and endpoints."""

    request_timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    open_interest_history_period: str = "1h"
    open_interest_history_limit: int = 24
    long_short_period: str = "1h"
    orderbook_depth: int = 50
    news: NewsConfig = NewsConfig()
    fear_greed_url: str = "https://api.alternative.me/fng/"
    coingecko_global_url: str = "https://api.coingecko.com/api/v3/global"
    frankfurter_url: str = "https://api.frankfurter.dev/v1/latest"


class FeaturesConfig(_Strict):
    """M2 feature engine. Regime and S/R parameters are owner-approved (2026-08-18);
    the specs do not define them, so they live here rather than in code."""

    rsi_period: int = 14
    atr_period: int = 14
    ema_periods: tuple[int, ...] = (20, 50, 200)
    relative_volume_lookback: int = 20
    #: Indicators run on closed candles only; the in-progress bar is dropped.
    drop_partial_candle: bool = True

    primary_timeframe: str = "1h"
    context_timeframe: str = "4h"

    # Volatility regime: ATR% ranked against its own trailing distribution.
    volatility_lookback: int = 100
    volatility_min_observations: int = 20
    volatility_low_percentile: float = 33.0
    volatility_high_percentile: float = 67.0

    # Support/resistance: confirmed fractal pivots clustered into ATR-wide bands.
    pivot_window: int = 3
    level_cluster_atr_multiple: float = 0.5
    max_levels_per_side: int = 3
    level_timeframes: tuple[str, ...] = ("1h", "4h")


class ChartsConfig(_Strict):
    """M3 chart renderer. Sized for the analyst's vision input, not for a screen.

    1600x1000 keeps the long edge under the 2576px high-resolution limit for
    claude-fable-5, so nothing is downscaled server-side (downscaling is what
    blurs axis numbers and level labels). ~1.6MP is roughly 2,130 visual tokens
    per chart, ~6.4k for the three-chart album.
    """

    width_px: int = 1600
    height_px: int = 1000
    dpi: int = 100
    volume_panel_ratio: float = 0.22
    #: Legibility, not data: 200 candles across the plot area is ~7px each and
    #: bodies merge; 120 gives ~12px and a doji stays distinguishable.
    candle_window: int = 120
    ema_periods: tuple[int, ...] = (20, 50, 200)
    max_levels: int = 6
    timeframes: tuple[str, ...] = ("15m", "1h", "4h")
    output_dir: str = "charts_out"


class TrackerConfig(_Strict):
    """M7's outcome tracker — how it looks at price (ARCHITECTURE.md §3).

    Fills and stop-outs are detected from **1m candle high/low since the last
    tick**, not from the mark price polled every 60s. A poll misses the wick that
    actually filled the rung or hit the stop, and that error is not symmetric: it
    under-reports stop-outs, which flatters the measured win rate the whole system
    exists to produce. Candles are also replayable, so a detection bug can be
    reproduced from stored OHLCV instead of from a moment that has passed.

    Invalidation is the exception and stays on **closed 1h candles**, because
    specs/TELEGRAM_UX.md §4 words it as a close ("1h close 81.05 < 81.40") and
    specs/PROMPTS.md §2 rule e tells the analyst to treat wick-only breaches as
    noise. A tick is not an invalidation.
    """

    fill_timeframe: str = "1m"
    #: How far back a tick may look. Bounds the request when a tick was missed;
    #: a longer gap is covered by ``last_checked_at`` up to this ceiling.
    fill_lookback_candles: int = 5
    invalidation_timeframe: str = "1h"


class RiskConfig(_Strict):
    """specs/RISK_ENGINE.md sections 1-4. ``capital_eur`` is not here: set via /capital."""

    risk_per_trade_pct: Dec = Decimal("0.75")
    risk_per_trade_min_pct: Dec = Decimal("0.25")
    risk_per_trade_max_pct: Dec = Decimal("1.5")
    max_open_risk_pct: Dec = Decimal("2.25")
    max_leverage: int = 10
    margin_budget_pct: Dec = Decimal("10")
    daily_loss_limit_pct: Dec = Decimal("3.0")
    max_positions: int = 4
    min_rr_tp1: Dec = Decimal("1.5")
    max_entry_distance_pct: Dec = Decimal("3.0")
    min_confidence: int = 60
    stop_atr_min_multiple: Dec = Decimal("0.6")
    stop_atr_max_multiple: Dec = Decimal("3.0")
    min_rung_notional_usdt: Dec = Decimal("20")
    liq_buffer_multiple: Dec = Decimal("2.0")
    signal_cooldown_hours: int = 4
    #: specs/TELEGRAM_UX.md §6 — "hard cap max_signals_per_day (default 5)". The
    #: spec named it from the start; M7 is the first milestone with a scheduler
    #: that could exceed it, so it becomes a rail here rather than a promise.
    max_signals_per_day: int = 5


class CostsConfig(_Strict):
    """specs/RISK_ENGINE.md §4.2 (correction 2026-08-18) — transaction costs.

    Binance USDⓈ-M VIP 0, verified against the published schedule on 2026-08-18
    (https://www.binance.com/en/fee/futureFee): maker 0.0200%, taker 0.0500%.
    The BNB discount and the VIP tiers are deliberately not modelled — a cost
    estimate that flatters the trade is worse than no estimate at all.

    Entries are the maker leg (the ladder is limit orders); **every** exit is
    priced as taker, including a TP that might well rest as a limit — the same
    conservative bias as flooring quantities and rounding stops away from entry.
    """

    maker_fee_pct: Dec = Decimal("0.02")
    taker_fee_pct: Dec = Decimal("0.05")
    #: Most USDⓈ-M perps settle every 8h; some settle every 4h, and a contract at
    #: its funding cap drops to 1h. ccxt's premiumIndex response does not carry
    #: the interval, so this is an estimate — hence "est" on every funding line.
    funding_interval_hours: int = 8
    #: Funding is a cost for a long at a positive rate and a credit for a short.
    #: False means the gate uses ``max(0, funding)``: a credit is displayed but is
    #: never allowed to push a plan over ``min_rr_tp1``.
    credit_favourable_funding: bool = False


class LadderConfig(_Strict):
    """specs/RISK_ENGINE.md §3."""

    single_entry_atr_threshold: Dec = Decimal("0.5")
    weights_pct: tuple[Dec, ...] = (Decimal("40"), Decimal("35"), Decimal("25"))


class ManagementConfig(_Strict):
    """specs/RISK_ENGINE.md §5."""

    tp1_close_pct: Dec = Decimal("40")
    tp2_close_pct: Dec = Decimal("35")
    tp3_trail_atr_multiple: Dec = Decimal("1.0")
    entry_ttl_hours_intraday: int = 12
    entry_ttl_hours_swing: int = 36


class ModelPricing(_Strict):
    """USD per 1M tokens, per model.

    Published rates are in no spec, so they live in config where they can be
    corrected without a code change. **Token counts are the ground truth**; the
    money figure derived from them is an estimate, which is why every column and
    field carrying it is named ``cost_usd_estimate``.
    """

    input_per_mtok: Dec
    output_per_mtok: Dec
    cache_read_per_mtok: Dec = Decimal("0")
    cache_write_per_mtok: Dec = Decimal("0")


class LLMConfig(_Strict):
    """ARCHITECTURE.md §2. Prompt *text* lives only in analyst/prompts/ files.

    Two independent retry budgets, deliberately not merged — CLAUDE.md requires
    the first, specs/PROMPTS.md §2 the second:

    * ``max_transport_retries`` — 429/5xx/connection failures, handled by the SDK.
    * ``max_json_retries`` — schema-invalid *content*: retried once with the
      validation errors fed back, then discarded. Never guessed (ARCHITECTURE §6).
    """

    screener_model: str = "claude-sonnet-4-6"
    #: Which prompt file the screener loads (M8.2). A config value rather than a
    #: module constant so v1 and v2 can be compared without a deploy, and so a
    #: regression is a one-line rollback. Every `llm_calls` row already stores the
    #: version it used, so /stats can compare them retroactively.
    screener_prompt_version: str = "screener_v2"
    analyst_model: str = "claude-fable-5"
    analyst_effort: Literal["low", "medium", "high"] = "high"
    max_json_retries: int = 1

    #: Transport-level only. Content failures use ``max_json_retries``.
    max_transport_retries: int = 2
    screener_timeout_seconds: float = 60.0
    #: specs/ENSEMBLE.md §3 budgets 90s per provider; raised to 150s by owner
    #: ruling 2026-08-18 and recorded there as a dated correction. M5's live
    #: analyst calls ran 47-85s — 90s left almost no headroom, and M10 runs two
    #: providers in parallel. The analyst thinks before it answers, so this is a
    #: whole-turn budget, not a connect timeout.
    analyst_timeout_seconds: float = 150.0
    screener_max_tokens: int = 4096
    analyst_max_tokens: int = 16000
    #: 0 = one call for the whole watchlist (specs/PROMPTS.md §1 is a batch pass).
    screener_batch_size: int = 0
    #: specs/PROMPTS.md §3 — last N verdicts per symbol in the history block.
    history_verdicts: int = 3

    #: Spend guard (M7, pulled forward from M8's "spend guard" item). Once the
    #: scheduler runs unattended around the clock, a bug or a market event that
    #: makes many symbols look interesting can spend real money while nobody is
    #: watching. Reaching the daily limit suspends **new deep analysis only** —
    #: the screener keeps triaging and the tracker keeps managing open positions,
    #: which it can do without an LLM at all.
    #:
    #: Both figures gate ``cost_usd_estimate``, which is an estimate and not
    #: billing truth (journal/M5_REPORT.md §7). Sizing the default: the screener
    #: alone is ~$0.023 per cycle, so ~$2.2/day at 96 cycles, leaving ~$7.8 for
    #: deep analysis — roughly 24 analyst calls at M5's measured ~$0.32.
    daily_spend_limit_usd: Dec = Decimal("10")
    daily_spend_warn_usd: Dec = Decimal("7")

    pricing: dict[str, ModelPricing] = {
        "claude-sonnet-4-6": ModelPricing(
            input_per_mtok=Decimal("3"),
            output_per_mtok=Decimal("15"),
            cache_read_per_mtok=Decimal("0.3"),
            cache_write_per_mtok=Decimal("3.75"),
        ),
        "claude-fable-5": ModelPricing(
            input_per_mtok=Decimal("10"),
            output_per_mtok=Decimal("50"),
            cache_read_per_mtok=Decimal("1"),
            cache_write_per_mtok=Decimal("12.5"),
        ),
    }


class AlertsConfig(_Strict):
    """ARCHITECTURE.md §2 and §6 — "Telegram admin alert after 3 consecutive
    cycle failures". The decision itself is pure and lives in ``core/alerts.py``.

    ``stale_cycle_multiplier`` is how many scan intervals a cycle may stay
    ``RUNNING`` before it is counted as a failure. A cycle is budgeted five
    minutes against a fifteen-minute period (PRD F1), so two intervals is
    generous; the case it catches is a process killed mid-cycle, which leaves
    ``RUNNING`` in the row for ever and is otherwise invisible to a status check.
    """

    consecutive_cycle_failures: int = Field(default=3, ge=1)
    stale_cycle_multiplier: int = Field(default=1, ge=1)


class TelegramConfig(_Strict):
    """specs/TELEGRAM_UX.md. Display and delivery only — the allowlist is a secret."""

    owner_timezone: str = "UTC"
    digest_hour_local: int = 8
    quiet_mode_on_no_setup: bool = True
    #: §1 attaches the 1h and 4h charts to a card. The analyst still sees all three
    #: (15m included) — this is what the *owner* gets, and the 15m timing detail is
    #: already encoded in the entry zone.
    card_chart_timeframes: tuple[str, ...] = ("1h", "4h")
    #: Long polling. 30s is aiogram's own default and keeps the bot responsive
    #: without a webhook, which would need an inbound port on the VPS.
    poll_timeout_seconds: int = 30
    #: Cards use a handful of <b>/<i> tags; analyst prose is escaped before it is
    #: interpolated (see sentinel/bot/formatting.py).
    parse_mode: str = "HTML"


#: How Saxo names each timeframe on ``/chart/v3/charts`` (docs/specs/FOREX.md §4.4).
#: Every value we need — 1, 15, 60, 240, 1440 — is in Saxo's allowed ``Horizon`` enum,
#: verified live in journal/M10b_SPIKE.md §E3.
SAXO_HORIZONS: dict[str, int] = {"1m": 1, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


class ForexConfig(_Strict):
    """Everything the forex market needs that crypto has no equivalent of (M10b).

    A **top-level block**, not fields on :class:`MarketConfig`. `MarketConfig` holds
    what two markets would *disagree* about; this holds what only one of them has at
    all — a pip grace period, a rollover hour, a swap table, a spread threshold. Put
    on `MarketConfig` they would sit on crypto as dead keys inviting somebody to give
    them meaning.

    Nothing here is reachable while ``markets.forex.enabled`` is false.
    """

    #: LIVE. SIM is deliberately absent: journal/M10b_SPIKE.md found SIM's spread is a
    #: constant 2.0 pips bolted onto a mid series, so a SIM run would measure a
    #: fiction — and every cost number in §7 rests on the spread being real.
    base_url: str = "https://gateway.saxobank.com/openapi"
    token_url: str = "https://live.logonvalidation.net/token"
    authorize_url: str = "https://live.logonvalidation.net/authorize"

    #: Tail lengths, in candles, per timeframe. Same +1 convention as crypto: the
    #: newest bar is still forming and is dropped, so 321 requested is 320 closed.
    #:
    #: **1h asks for 1200, not 321** (owner correction, 2026-08-21). Features use the
    #: most recent 321 of that tail exactly as before; the extra history exists for
    #: the hour-of-day spread profile, where 321 bars is ~13 days and leaves ~10
    #: samples per hour after weekends. A median over 10 noisy samples is not a
    #: baseline, least of all in the tail hours where it decides whether a signal is
    #: emitted. 1200 is ~50 days and ~35 samples per hour — what the spike measured —
    #: and it is still **one** request, sitting exactly at the ceiling rather than
    #: over it.
    timeframes: tuple[TimeframeSpec, ...] = (
        TimeframeSpec(timeframe="15m", candles=321),
        TimeframeSpec(timeframe="1h", candles=1200),
        TimeframeSpec(timeframe="4h", candles=321),
        TimeframeSpec(timeframe="1d", candles=101),
    )
    #: How many of the 1h tail the feature engine sees. Keeping this equal to crypto's
    #: 321 is what makes "the 1h tail got longer" a spread-profile change and not a
    #: feature change.
    feature_candles_1h: int = Field(default=321, ge=2)

    #: Saxo's documented ``Count`` ceiling. Over-requesting **clamps silently** to it
    #: (D-e), which is why every read asserts the count it got.
    max_count: int = Field(default=1200, ge=1)

    #: Seconds after a bar's nominal close before it is treated as closed (§4.1).
    #: There is no closed flag, so this clock rule is the only thing standing between
    #: a forming bar and every indicator on the newest candle. Measured publish lag
    #: was 0-4 s across four rolls, with 5 s of poll pessimism on top: 9 s worst
    #: observed, and 30 s is ~3x that. Being generous costs nothing against a
    #: 60-minute bar; being tight poisons an indicator silently.
    candle_grace_seconds: int = Field(default=30, ge=0)

    #: How far back the hour-of-day spread profile looks, in 1h candles. Separate
    #: from the tail length so the profile can be widened or narrowed without moving
    #: what the feature engine sees.
    spread_lookback_candles: int = Field(default=1200, ge=1)

    #: How many times an instrument's **global** median spread the current spread may
    #: reach before a new signal is refused (§5.3, owner correction C2, 2026-08-21).
    #:
    #: Global, not per-hour-of-day: a per-hour baseline makes GBPUSD's 12.0-pip 21:00
    #: median "normal for that hour" and never fires at the one hour it exists for.
    #: See sentinel/fx/spread.py.
    #:
    #: 3.0 is a **starting guess to be calibrated from DRY_RUN data**, not a derived
    #: figure. Sanity-checked against journal/M10b_SPIKE.md §3: EURUSD's global median
    #: of 1.1 gives a 3.3-pip threshold, not reached in normal London/New York hours
    #: and exceeded at rollover; GBPUSD's 1.8 gives 5.4, which its 21:00 median of 12.0
    #: fails. Every firing is logged with instrument, hour, spread and threshold,
    #: because how often it fires is itself a measurement.
    spread_max_multiple: Dec = Decimal("3.0")
    #: Below this many samples the profile is not trusted and the clock backstop is
    #: used instead. ~35 samples per hour-of-day come out of a 1200-bar tail.
    spread_min_samples: int = Field(default=30, ge=1)

    # ── the trading week (§5) ────────────────────────────────────────────────
    #
    # All UTC, and all **nominal**. The real boundary moves by an hour twice a year
    # because the US and EU change daylight saving on different dates, so these are a
    # sanity check against what the candles actually show, never the authority. See
    # sentinel/fx/hours.py, which derives the week open from candle availability and
    # logs any divergence from these.

    #: Sunday. journal/M10b_SPIKE.md §6 confirms the last Friday bar is stamped
    #: 20:00Z (covering 20:00-21:00), so the week closes at 21:00 UTC.
    week_open_hour_utc: int = Field(default=21, ge=0, le=23)
    week_close_hour_utc: int = Field(default=21, ge=0, le=23)

    #: How long after the week opens before a signal may be emitted (§5.2). D-d
    #: leaves the exact Sunday open ambiguous -- 19:00Z or 21:00Z depending on how
    #: the question is asked -- and Sunday-evening liquidity is thin regardless, so
    #: nothing is lost by staying quiet through the ambiguity.
    week_open_quiet_hours: int = Field(default=3, ge=0)

    #: No new signals after this hour on Friday (§5.4). A ladder placed later cannot
    #: fill before the weekend, and a position that does fill carries gap risk
    #: through it -- neither has any analogue in a 24/7 market.
    friday_signal_cutoff_hour_utc: int = Field(default=19, ge=0, le=23)

    #: The hour on Friday at which every still-pending entry ladder **expires** (§5.4,
    #: §16.10). A ladder cannot fill over a weekend and a rung that filled on the
    #: Sunday open would fill against a gap nobody's stop was placed for, so a pending
    #: ladder is expired with a reason the owner sees rather than paused.
    #:
    #: One hour before ``week_close_hour_utc`` rather than derived from it, because
    #: the derivation would have to survive the DST shift §5.2 describes and an
    #: explicit hour does not. It must stay **below** the close; a validator enforces
    #: that rather than trusting two numbers to be edited together.
    friday_ladder_expiry_hour_utc: int = Field(default=20, ge=0, le=23)

    # ── rollover (§5.3) ──────────────────────────────────────────────────────
    #
    # The primary rail is spread-triggered (see spread_max_multiple). This clock
    # window is the **backstop** for when the measured spread series is unavailable.

    #: Swap is charged here, and tripled on Wednesday.
    rollover_hour_utc: int = Field(default=21, ge=0, le=23)
    #: Half-open on bar stamps: 19 to 22 covers the bars stamped 19, 20 and 21, which
    #: is wall-clock 19:00-22:00. journal/M10b_SPIKE.md §3 measures elevated spreads
    #: across exactly those three bars (GBPUSD median 12.0 pips at 21:00) and normal
    #: ones again at 22:00. v1's +/-15 minutes was far too narrow.
    rollover_window_start_hour_utc: int = Field(default=19, ge=0, le=23)
    rollover_window_end_hour_utc: int = Field(default=22, ge=1, le=24)

    #: The UTC hours forex is scanned, half-open — ``[7, 19)`` is 07:00 through 18:59,
    #: twelve hourly cycles covering London and the New York morning.
    #:
    #: **It ends at 19 because of evidence quality, not cost** (spec defect #30, owner
    #: decision 2026-08-21). D-g measured elevated spreads from 19:00 to 21:00, and
    #: §5.3's spread-triggered rail provably does not reject them: ``spread_gate``
    #: reaches its clock backstop only when the series is *unusable*, which at a
    #: 1200-bar tail it never is, so ``ROLLOVER_WINDOW`` is unreachable in production
    #: and the measured rail decides alone — at 3.0x the global median the thresholds
    #: are 3.3/5.4/4.5 pips against 19:00-20:00 hour medians of 1.1-1.8 and 1.5-4.6, so
    #: every one of them passes. Those two cycles bought the day's worst evidence at
    #: full price with the rail written to stop exactly that letting them through. 19
    #: invents nothing: it is already ``friday_signal_cutoff_hour_utc``.
    #:
    #: **It is also a spend rail**, and that is the secondary benefit. Forex has no
    #: screener and no usable re-analysis cooldown (crypto's two M8.2 cost controls),
    #: so every cycle is three claude-fable-5 calls unconditionally — measured at
    #: $0.734 a cycle, so twelve cycles is ~$8.81 a trading day and twenty-four would
    #: be ~$17.6. Outside these hours the cycle returns before the first fetch, so
    #: those hours cost **zero** rather than cheap.
    scan_hours_utc: tuple[int, int] = (7, 19)

    # ── authentication (§3) ──────────────────────────────────────────────────

    #: How often the refresh job runs, in seconds. Measured access-token lifetime is
    #: ~20 minutes (1070-1200 s observed, and it VARIES between responses), so five
    #: minutes sits well inside it. The cadence is not really about the access token
    #: though: every refresh resets the refresh token's one-hour life, and that hour
    #: is the whole margin between a restart that survives and one that needs a human.
    token_refresh_interval_seconds: int = Field(default=300, ge=30)
    #: Refresh early if the access token is inside this margin of expiring, so a call
    #: is never made with a token that dies mid-flight.
    token_expiry_margin_seconds: int = Field(default=120, ge=0)

    # ── the economic calendar (§8) ───────────────────────────────────────────
    #
    # There is NO reachable Saxo calendar: the feature flag says Calendar: true and
    # all five probed paths 404 (journal/M10b_SPIKE.md §5). The hand-maintained YAML
    # at sentinel/fx/data/calendar.yaml is not a backstop, it is the only source, and
    # keeping it current is real recurring manual work.

    #: Suppress new signals from this many minutes before a high-impact event...
    blackout_before_minutes: int = Field(default=60, ge=0)
    #: ...to this many minutes after it. Owner decision, 2026-08-21.
    blackout_after_minutes: int = Field(default=30, ge=0)
    #: Alert when the calendar's coverage runs out within this many days. It is a
    #: warning, not a suppression -- suppression happens once coverage has actually
    #: lapsed, and the point of the warning is that the owner hears about it before
    #: that morning rather than on it.
    calendar_warn_within_days: int = Field(default=14, ge=0)

    # ── transaction costs (§7.3) ─────────────────────────────────────────────
    #
    # The spread is MEASURED, not configured -- it is the one thing forex has that
    # crypto's order book was standing in for, and journal/M10b_SPIKE.md confirmed the
    # chart spread against a firm dealable quote. Only the two things the chart cannot
    # tell us live here.

    #: Commission per 1,000,000 units of quote-currency notional, per leg. Saxo's
    #: standard FX spot pricing is spread-only, so this is 0 -- a **configured** zero
    #: for an account-specific fee, not a missing measurement standing in as one. Set
    #: it if the account has a commission schedule.
    commission_per_million_quote: Dec = Decimal("0")

    #: Swap, in pips per night, per instrument per direction. Charged at
    #: ``rollover_hour_utc`` Monday to Friday and TRIPLED on Wednesday, which is how
    #: the market settles the coming weekend in advance.
    #:
    #: Empty by default and that is deliberate: swap rates are account- and
    #: date-specific, they are published by the broker rather than derivable from the
    #: chart, and a plausible-looking guess here would be a fabricated cost. An
    #: instrument with no entry is charged nothing and the report says which.
    swap_pips_per_night: dict[str, dict[str, Dec]] = {}

    #: Whether a swap CREDIT may improve net RR. False means the gate charges
    #: ``max(0, rollover)``: the credit is still shown, but it can never be the reason
    #: a plan clears the threshold. Same rule and same reasoning as crypto's
    #: ``credit_favourable_funding`` (specs/RISK_ENGINE.md §4.2).
    credit_favourable_rollover: bool = False

    # ── sizing and margin (§7.6) ─────────────────────────────────────────────

    #: Maximum leverage, and it is an **assumption**, not a reading. Spike defect D-f:
    #: the instrument details response carries no ``MarginRates`` and no
    #: ``MarginTiers``, so FOREX.md v1's "read the venue's actual leverage figure" is
    #: not satisfiable from that endpoint. 30:1 is the ESMA retail cap on major pairs.
    #: sentinel/fx/sizing.py carries the words with the number, so a surface cannot
    #: show the figure without the caveat.
    max_leverage: int = Field(default=30, ge=1)
    #: Ceiling on total forex margin as a share of equity (§7.6's rail).
    max_margin_pct_of_equity: Dec = Decimal("20")
    #: §9. One open forex position at a time for the first measurement window.
    #: EURUSD, GBPUSD and USDJPY all cross the dollar, so long EURUSD plus short
    #: USDJPY is one large short-dollar bet wearing two hats and the existing rails
    #: would allow both. Crude, safe, and it makes the first numbers interpretable.
    max_concurrent_positions: int = Field(default=1, ge=1)

    # ── setup quality (§16.5 rows 5 and 6; spec defect #21) ──────────────────
    #
    # FOREX.md §7 and §9 name the sizing rails and the correlation cap and say
    # nothing about setup QUALITY, so these had no home and the obvious move was to
    # read crypto's `RiskConfig`. One of those numbers is actively wrong here:
    # `risk.max_entry_distance_pct` is 3.0, and EURUSD moves about 0.5% in a day, so a
    # 3% bound could essentially never fire. A rail that cannot fire is worse than an
    # absent one, because it reads on a checklist as a rail.
    #
    # So forex gets its own, and crypto's `risk:` block is untouched. Every number
    # below is a STARTING GUESS to be calibrated from DRY_RUN data, in exactly the
    # same standing as `spread_max_multiple: 3.0` above — not a derived figure.

    #: Minimum **net** reward-to-risk at TP1, after the measured spread, commission and
    #: rollover. 1.5 matches crypto's, and it is the one number here with an argument
    #: behind it rather than a guess: §7.4 computes what gross RR a forex setup needs
    #: to clear a 1.5 net gate, and journal/M10b_REPORT.md §6 measured 1.65-1.76 at the
    #: widest stop each pair can size at €200. Keeping the two markets' gates at the
    #: same net level is what makes their statistics comparable at all.
    min_rr_tp1: Dec = Decimal("1.5")
    #: Below this, a candidate is DOWNGRADED to watchlist rather than rejected.
    min_confidence: int = Field(default=60, ge=0, le=100)
    #: How far either edge of the entry zone may sit from the last price. **0.5, not
    #: crypto's 3.0** — see the note above; this is the number defect #21 is about.
    max_entry_distance_pct: Dec = Decimal("0.5")
    #: Stop distance bounds as multiples of ATR(1h), same shape as crypto's §2 rules
    #: 3 and 4. Carried over unchanged because they are expressed in ATR, which is
    #: already the instrument's own volatility — that is what makes them transferable
    #: where a percentage is not.
    stop_atr_min_multiple: Dec = Decimal("0.6")
    stop_atr_max_multiple: Dec = Decimal("3.0")
    #: How long a symbol stays quiet after a resolved signal.
    signal_cooldown_hours: int = Field(default=4, ge=0)
    #: Anti-spam cap. Three, not crypto's five, because there are three instruments:
    #: a fourth forex signal in a day means the same dollar view arriving twice, which
    #: is what §9's correlation cap exists to prevent one level down.
    max_signals_per_day: int = Field(default=3, ge=1)

    #: How many timeframes old the newest closed candle may be **while the market is
    #: open** before the symbol is skipped. Checked only when open: a weekend read is
    #: hours stale by construction and that is not a fault. See journal/M10b_REPORT.md
    #: on spec defect #14 -- FOREX.md §5.1 expected crypto's staleness rule to misfire
    #: over the weekend, when in fact it compares ``fetched_at`` and would have
    #: reported a frozen weekend snapshot as perfectly fresh.
    max_candle_age_multiplier: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def _the_scan_window_is_a_window(self) -> ForexConfig:
        """Two numbers a person edits one at a time, and both ways of getting it
        wrong are silent.

        ``[21, 7)`` reads like "overnight" and means "never" to a half-open
        comparison, so forex would run no cycles at all and look like a quiet market.
        ``[0, 24)`` is legitimate and is how somebody turns the window off.
        """
        start, end = self.scan_hours_utc
        if not 0 <= start < end <= 24:
            raise ValueError(
                f"forex.scan_hours_utc must be [start, end) with 0 <= start < end <= 24, "
                f"got [{start}, {end}). A window that does not increase never opens, and "
                f"a forex market that is never scanned is indistinguishable from a quiet one."
            )
        return self

    @model_validator(mode="after")
    def _ladders_expire_before_the_week_closes(self) -> ForexConfig:
        """§5.4 is a promise about *ordering*, so it is checked rather than trusted.

        ``friday_ladder_expiry_hour_utc`` and ``week_close_hour_utc`` are two numbers a
        person edits one at a time. Set the first at or after the second and the rail
        silently stops existing: the market shuts, the tracker stops ticking, and the
        ladder that was supposed to expire before the close is still pending on Sunday
        evening — carrying exactly the gap risk §5.4 exists to remove, with no error
        anywhere and nothing on any surface to say so.
        """
        if self.friday_ladder_expiry_hour_utc >= self.week_close_hour_utc:
            raise ValueError(
                f"forex.friday_ladder_expiry_hour_utc "
                f"({self.friday_ladder_expiry_hour_utc}:00Z) must be BEFORE "
                f"forex.week_close_hour_utc ({self.week_close_hour_utc}:00Z) — "
                f"FOREX.md §5.4 requires every pending ladder to expire before the "
                f"Friday close, and at or after it the ladder simply survives the "
                f"weekend instead"
            )
        return self


#: The adapter name M10a ships. ``forex_saxo`` names
#: :class:`~sentinel.ingestion.adapters.forex_saxo.SaxoForexAdapter` from M10b-1 —
#: which exists, is tested, and is **not wired into a cycle**. The orchestrator still
#: builds only the Binance adapter; M10b-2 adds the second path. So the name resolves
#: to real code and to no behaviour, which is exactly what shipping behind
#: ``enabled: false`` is supposed to mean.
CRYPTO_ADAPTER = "crypto_binance"

#: The forex adapter, registered in :mod:`sentinel.core.wiring` from M10b-2. M10a
#: deliberately left this name unregistered so a market claiming it would fail loudly
#: rather than silently do nothing; M10b-1 built the adapter behind it; M10b-2 wires
#: it up. The loud-failure property survives — an *unknown* name still raises — which
#: is the case that actually happens, because it is a typo.
FOREX_ADAPTER = "forex_saxo"


class MarketConfig(_Strict):
    """One market's own settings (M10a).

    Everything here used to be a single global value, and every one of them is a
    thing two markets would disagree about. A watchlist is obviously per market. So
    is ``dry_run``: the whole point of a second market is that it can rehearse for a
    fortnight while crypto keeps posting real cards, and one global flag makes that
    impossible without a deploy in between.

    ``llm_daily_budget_usd`` is this market's own daily ceiling. It sits under
    :attr:`AppConfig.llm_daily_budget_global_usd`, and the two are deliberately not
    consistent — the sub-budgets sum to more than the ceiling, so markets compete for
    the last dollar instead of reserving it (specs: M10a Step 4).
    """

    #: A disabled market is not scheduled, not screened and not spent on. It exists
    #: in config so the shape is reviewable before the code that fills it lands.
    enabled: bool = True
    #: Run the full cycle and publish **nothing** — M7's flag, now per market.
    dry_run: bool = False
    #: Which :mod:`sentinel.core.wiring` adapter ingests it.
    adapter: str = CRYPTO_ADAPTER
    watchlist: tuple[str, ...] = ()
    #: The hard ceiling on watchlist size. Per market because it is a spend control
    #: and the spend is per market.
    watchlist_max_symbols: int = Field(default=15, ge=1)
    scan_interval_minutes: int = Field(default=60, ge=1)
    #: This market's slice of the day's LLM budget, and the level at which it warns.
    llm_daily_budget_usd: Dec = Decimal("10")
    llm_daily_warn_usd: Dec = Decimal("7")
    #: Which analyst prompt this market's deep analysis uses. Same pattern as
    #: :attr:`adapter` — a name in config, resolved in one place — because the
    #: alternative is a market check inside the provider, and the provider is
    #: deliberately market-blind (it takes a snapshot, not a market).
    #:
    #: Provider-major filename per specs/ENSEMBLE.md §2, market as suffix; settled
    #: with the owner 2026-08-21 as spec defect #19. Crypto keeps ``fable_v1``, whose
    #: bytes the golden prompt pins.
    analyst_prompt_version: str = "fable_v1"
    #: How much of the global ceiling this market keeps for itself (FOREX.md §13
    #: decision 6, M10a open question R5, applied in M10b-2).
    #:
    #: The sub-budgets deliberately sum to more than the ceiling so markets compete
    #: for the last dollar. That is the right default between two *measured* markets
    #: and the wrong one the day an unmeasured market joins a measured one: a
    #: forex-heavy morning could spend the shared dollar and leave crypto — which has
    #: a live measurement window running — short. A floor says "this much is not
    #: available to anyone else", and only the **unspent** part of it is held, so a
    #: market that does not use its floor stops reserving it.
    #:
    #: Zero, the default, is exactly the pre-M10b-2 behaviour.
    llm_reserved_floor_usd: Dec = Decimal("0")


#: The keys ``markets:`` replaced. A config file carrying these and no ``markets``
#: block is a pre-M10a file, and is read as crypto-only.
#:
#: **Correction (2026-08-21, hygiene session).** This used to say the deployed server
#: runs such a file, because docs/DEPLOY.md §6/§13 edit ``config.yaml`` in place. It
#: does not, and they do not: the server's ``config.yaml`` is byte-identical to the
#: committed one and has carried a ``markets:`` block since some earlier deploy
#: (verified on the live box 2026-08-21). §6/§13 could not have edited it in place
#: anyway — the config is baked into the image at build time. The backward-compatible
#: read below is kept for old files and for the frozen test fixture, not because any
#: running deployment needs it.
LEGACY_MARKET_KEYS = ("dry_run", "watchlist", "watchlist_max_symbols")


#: The crypto watchlist as it has shipped since M0 (PRD.md §7). Lives here rather
#: than inline on :class:`MarketConfig` because it is also the value the legacy
#: synthesis below falls back to when a pre-M10a config names no watchlist at all.
DEFAULT_CRYPTO_WATCHLIST = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "ADAUSDT",
)


def normalise_markets(data: dict[str, Any]) -> dict[str, Any]:
    """Turn a pre-M10a mapping into a ``markets:``-shaped one. Never mutates.

    Two shapes are valid and one is not:

    * **No ``markets`` key.** Every pre-M10a config, including the one the live
      server is running right now. ``dry_run``, ``watchlist`` and
      ``watchlist_max_symbols`` become ``markets.crypto``'s; the scan interval comes
      from ``schedule`` and the budget from ``llm.daily_spend_limit_usd`` — so the
      deployed spend rail survives the migration at exactly the number it has today.
    * **A ``markets`` key and none of those.** The post-M10a shape.
    * **Both.** Refused. Two places to set ``dry_run`` is one place for it to
      disagree, and the losing one would be silent — which on that particular flag
      means publishing real cards during a rehearsal.

    The refusal is about a *file* declaring both shapes. It is not in the way of
    ``DB > yaml`` precedence: :func:`load_config` normalises the yaml first and
    merges the database's overrides — which are always new-shaped — on top of the
    result, so a stored watchlist still wins over a legacy file's.
    """
    present = [key for key in LEGACY_MARKET_KEYS if key in data]
    if "markets" in data:
        if present:
            raise ValueError(
                "config has a 'markets:' block and also the pre-M10a top-level "
                f"key(s) {', '.join(present)}. Move them under markets.crypto — two "
                "places to set the same value is one place for it to be wrong."
            )
        return data
    if not present:
        return data

    data = dict(data)
    legacy = {key: data.pop(key) for key in present}
    schedule = data.get("schedule") or {}
    llm = data.get("llm") or {}
    data["markets"] = {
        LEGACY_MARKET.value: {
            "enabled": True,
            "adapter": CRYPTO_ADAPTER,
            "dry_run": legacy.get("dry_run", False),
            "watchlist": legacy.get("watchlist", DEFAULT_CRYPTO_WATCHLIST),
            "watchlist_max_symbols": legacy.get(
                "watchlist_max_symbols", _default_of(MarketConfig, "watchlist_max_symbols")
            ),
            "scan_interval_minutes": schedule.get(
                "scan_interval_minutes", _default_of(ScheduleConfig, "scan_interval_minutes")
            ),
            "llm_daily_budget_usd": llm.get(
                "daily_spend_limit_usd", _default_of(LLMConfig, "daily_spend_limit_usd")
            ),
            "llm_daily_warn_usd": llm.get(
                "daily_spend_warn_usd", _default_of(LLMConfig, "daily_spend_warn_usd")
            ),
        }
    }
    return data


def _default_of(model: type[BaseModel], field: str) -> Any:
    """A field's declared default, read from the model rather than restated.

    :func:`normalise_markets` needs the defaults of fields it is *not* being given,
    and a second copy of "60" or "10" here would drift from the real one the first
    time somebody changed it in the obvious place.
    """
    return model.model_fields[field].get_default(call_default_factory=True)


class PersianSummaryConfig(_Strict):
    """The 🇮🇷 فارسی button (M11p). A convenience surface, budgeted like one.

    **Why its own rails rather than the analysis ones.** Persian summaries are
    user-initiated, so their volume is a bored thumb rather than a market. They are
    recorded in ``llm_calls`` like every other call, which means they are visible on
    ``/status`` and ``/pulse`` for free — and it also means, without a rail of their
    own, they would draw from the same global ceiling as deep analysis.

    ``markets.crypto.llm_reserved_floor_usd`` does **not** cover that. The floor holds
    the unspent part of crypto's budget against *other markets*; forex has no floor at
    all, and its real ceiling is ``llm_daily_budget_global_usd`` minus crypto's unspent
    floor -- $12 against a measured need of $8.81/day. A path spending from the ceiling
    eats forex's $3.19 of slack first. So the exposure is bounded here instead, by
    construction rather than by an average.

    :attr:`daily_generations_per_user` counts **generations, not presses**: a second
    press on the same card returns the stored text and costs nothing, and rationing a
    free action would be a rail that only annoys.
    """

    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    prompt_version: str = "persian_summary_v1"
    #: Persian is token-hungry; 700 output tokens comfortably covers the 900-character
    #: ceiling the prompt asks for, with headroom for a model that overruns it.
    max_output_tokens: int = 700
    #: A style ceiling, not a safety one: an overrun is logged and still sent. Telegram
    #: allows 4096. The number that matters is the thirty-second read, not the limit.
    max_output_chars: int = 900
    timeout_seconds: float = 60.0
    #: Both numbers are MEASURED-then-set, not estimated (journal/M10d_REPORT.md §8).
    #: One real call on the golden card, 2026-08-22, ``claude-sonnet-4-6``, cold cache:
    #: 594 in + 1,031 cache write / 436 out = **$0.012188** a press, 12.6 s, 611 chars.
    #:
    #: A press inside the five-minute cache window is much cheaper -- a second measured
    #: call read the system block back for $0.007071 -- so the rail is set on the COLD
    #: figure, which is the pessimistic one and the one an isolated press pays.
    #:
    #: 20 per user is the rail that bites: two approved users x 20 = **$0.4875**, with
    #: 0.60 above it so the per-user cap reliably exhausts first however long a card
    #: runs. The ceiling is the backstop for a third user rather than a second rail on
    #: the same two. 0.60 is 4.8% of an ordinary $12.42 day and 19% of the ~$3.19 of
    #: headroom forex has under the global ceiling after crypto's reserved floor --
    #: which is the number this is really protecting.
    daily_usd_cap: Dec = Decimal("0.60")
    daily_generations_per_user: int = 20
    #: A press already in flight is awaited rather than duplicated; this covers the
    #: narrower case of a second tap arriving just after the first one finished.
    double_tap_seconds: float = 5.0


class AppConfig(_Strict):
    """The whole non-secret runtime configuration."""

    #: Every market this deployment knows about (M10a), in the order the config
    #: names them — which is the order every multi-market surface renders in.
    #:
    #: A config with no ``markets:`` block is read as crypto-only and synthesised
    #: from the pre-M10a top-level keys; see :meth:`_carry_legacy_markets`.
    #:
    #: **Correction (2026-08-21, hygiene session).** This used to call that "the
    #: deployed reality". It is not: the live server loads a ``markets:``-shaped file
    #: identical to the committed one. See :data:`LEGACY_MARKET_KEYS` above.
    markets: dict[Market, MarketConfig] = Field(
        default_factory=lambda: {LEGACY_MARKET: MarketConfig(watchlist=DEFAULT_CRYPTO_WATCHLIST)}
    )
    #: The ceiling above every market's own budget (M10a Step 4). Reaching it stops
    #: new deep analysis in **all** markets. Deliberately below the sum of the
    #: sub-budgets, so a quiet market does not reserve money a busy one could use.
    llm_daily_budget_global_usd: Dec = Decimal("11")
    schedule: ScheduleConfig = ScheduleConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    ingestion: IngestionConfig = IngestionConfig()
    data_quality: DataQualityConfig = DataQualityConfig()
    features: FeaturesConfig = FeaturesConfig()
    charts: ChartsConfig = ChartsConfig()
    risk: RiskConfig = RiskConfig()
    tracker: TrackerConfig = TrackerConfig()
    costs: CostsConfig = CostsConfig()
    ladder: LadderConfig = LadderConfig()
    management: ManagementConfig = ManagementConfig()
    llm: LLMConfig = LLMConfig()
    telegram: TelegramConfig = TelegramConfig()
    alerts: AlertsConfig = AlertsConfig()
    #: M10b. Unreachable while ``markets.forex.enabled`` is false; present so the
    #: shape is reviewable and testable before the market is switched on.
    forex: ForexConfig = ForexConfig()
    #: M11p. Fully defaulted, so a config file written before this milestone still
    #: loads -- the same posture ``forex`` shipped with.
    persian_summary: PersianSummaryConfig = PersianSummaryConfig()

    @model_validator(mode="before")
    @classmethod
    def _carry_legacy_markets(cls, data: Any) -> Any:
        """Read a pre-M10a mapping as crypto-only. See :func:`normalise_markets`.

        On the model rather than only in :func:`load_config`, so it holds wherever
        an ``AppConfig`` is built — a test, a tool, a fixture — and not only on the
        one path that happens to read yaml.
        """
        if not isinstance(data, dict):
            return data
        return normalise_markets(data)

    @model_validator(mode="after")
    def _symbols_are_disjoint_across_markets(self) -> AppConfig:
        """No symbol may be watched by two markets (M10b, specs/FOREX.md §4.3).

        ``ohlcv_candles`` keeps its ``(symbol, timeframe, open_time)`` primary key —
        M10a deliberately did not widen it, because symbols are disjoint and
        rebuilding the largest table in the schema for a query nobody makes is a poor
        trade. ``bot/markets.market_of_symbol`` rests on the same assumption.

        M10a left "does the key have to grow before two markets can share a symbol
        string" as an open question for M10b. This is the answer: the assumption is
        **asserted at load** rather than widened, so an overlap is found by a config
        that refuses to load rather than by corrupted candles that upsert over each
        other with no error and no way back.

        Checked across **every configured market, enabled or not** — deliberately
        wider than the failure it prevents. An overlap introduced while forex is
        switched off is a trap set for the day it is switched on, and a config edit
        is the cheapest possible moment to hear about it.
        """
        seen: dict[str, Market] = {}
        for name, cfg in self.markets.items():
            for symbol in cfg.watchlist:
                owner = seen.get(symbol)
                if owner is not None:
                    raise ValueError(
                        f"symbol {symbol!r} is watched by both {owner.value} and "
                        f"{name.value}. Symbols must be disjoint across markets: "
                        f"ohlcv_candles is keyed on (symbol, timeframe, open_time) "
                        f"without the market, so two markets sharing one would upsert "
                        f"over each other silently."
                    )
                seen[symbol] = name
        return self

    @model_validator(mode="after")
    def _reserved_floors_fit_under_the_ceiling(self) -> AppConfig:
        """Floors must be satisfiable, and each must fit inside its own budget.

        Both halves are load-time errors rather than runtime surprises. Floors summing
        past the global ceiling is a deployment that can never satisfy its own
        promises; a floor above the market's own daily budget reserves money that
        market is not permitted to spend, which would starve every other market to
        hold a dollar nobody can use.
        """
        for name, cfg in self.markets.items():
            if cfg.llm_reserved_floor_usd > cfg.llm_daily_budget_usd:
                raise ValueError(
                    f"{name.value}: reserved floor ${cfg.llm_reserved_floor_usd} exceeds its "
                    f"own daily budget ${cfg.llm_daily_budget_usd} — it would reserve money "
                    f"this market is not allowed to spend"
                )
        total = sum((cfg.llm_reserved_floor_usd for cfg in self.markets.values()), start=Decimal(0))
        if total > self.llm_daily_budget_global_usd:
            raise ValueError(
                f"reserved floors sum to ${total}, above the global ceiling "
                f"${self.llm_daily_budget_global_usd} — the deployment could never honour them"
            )
        return self

    def market(self, market: Market = LEGACY_MARKET) -> MarketConfig:
        """One market's settings.

        Raises rather than returning a default for a market this deployment does not
        configure: a missing market is a wiring mistake, and inventing an empty
        watchlist for it would turn that mistake into a silently quiet cycle.
        """
        try:
            return self.markets[market]
        except KeyError:
            known = ", ".join(sorted(m.value for m in self.markets)) or "none"
            raise KeyError(
                f"no configuration for market {market.value!r} (configured: {known})"
            ) from None

    @property
    def enabled_markets(self) -> tuple[Market, ...]:
        """Every enabled market, in the order the config names them."""
        return tuple(name for name, cfg in self.markets.items() if cfg.enabled)

    @property
    def multi_market(self) -> bool:
        """Whether anything user-facing should say *which* market it is talking about.

        The single condition every market tag in ``sentinel/bot/`` hangs off, so
        "with forex disabled the cards are byte-identical to today" is one fact to
        check rather than a habit six renderers have to keep.
        """
        return len(self.enabled_markets) > 1


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``overlay`` onto ``base`` (``overlay`` wins)."""
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_config(
    path: Path | str | None = None,
    db_overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Build the effective config: **DB overrides > yaml > code defaults**.

    A missing yaml file is fine — the code defaults above are the spec defaults.
    An unknown or malformed key raises, rather than being silently ignored.
    """
    if path is not None:
        config_path = Path(path)
    else:
        config_path = Path(os.getenv("CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))

    raw: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} must contain a YAML mapping at the top level")
            raw = loaded

    # Normalise BEFORE the merge, not after. The database's overrides are always
    # new-shaped (``markets.crypto.watchlist``), and merging them onto a legacy file
    # would produce a mapping carrying both shapes at once — which
    # ``normalise_markets`` refuses, correctly, for a file. Normalising first means
    # ARCHITECTURE §2's "DB > yaml > defaults" still holds for a server whose
    # config.yaml has not been migrated yet.
    #
    # (Correction, 2026-08-21: that used to read "which is every server today". No
    # server is in that state — the live one runs the committed markets:-shaped file.
    # The normalise-first ordering is still correct for any file of either shape.)
    raw = normalise_markets(raw)
    if db_overrides:
        raw = _deep_merge(raw, db_overrides)

    return AppConfig.model_validate(raw)


class Settings(BaseModel):
    """Convenience bundle of both layers, built once at startup."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    secrets: Secrets = Field(repr=False)
    config: AppConfig


def load_settings(config_path: Path | str | None = None) -> Settings:
    secrets = Secrets()
    return Settings(secrets=secrets, config=load_config(config_path or secrets.config_path))
