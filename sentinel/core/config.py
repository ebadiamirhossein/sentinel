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
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    scan_interval_minutes: int = 15
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
    #: scheduler runs unattended every 15 minutes, a bug or a market event that
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
    stale_cycle_multiplier: int = Field(default=2, ge=1)


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


class AppConfig(_Strict):
    """The whole non-secret runtime configuration."""

    #: Run the whole cycle — ingestion, screener, charts, analyst, gate,
    #: persistence — and publish **nothing** to Telegram. The would-be card is
    #: rendered through the same renderer and logged verbatim, the signal is
    #: stored with ``dry_run=true``, and the tracker resolves it silently, so a
    #: day in this mode produces a measured paper record rather than only an
    #: absence of crashes. The first unattended run is the riskiest moment in the
    #: project; this is how it is made observable before it can talk.
    dry_run: bool = False

    watchlist: tuple[str, ...] = (
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
