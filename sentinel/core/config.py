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
    # Comma-separated in the environment; parsed by `allowed_user_ids`.
    telegram_allowed_user_ids: str = ""
    cryptopanic_api_key: SecretStr | None = None

    @property
    def allowed_user_ids(self) -> tuple[int, ...]:
        """Telegram allowlist (specs/TELEGRAM_UX.md §1). Everyone else gets silence."""
        raw = self.telegram_allowed_user_ids.strip()
        if not raw:
            return ()
        return tuple(int(part.strip()) for part in raw.split(",") if part.strip())

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
    timeframes: tuple[TimeframeSpec, ...] = (
        TimeframeSpec(timeframe="15m", candles=200),
        TimeframeSpec(timeframe="1h", candles=200),
        TimeframeSpec(timeframe="4h", candles=200),
        TimeframeSpec(timeframe="1d", candles=100),
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


class LLMConfig(_Strict):
    """ARCHITECTURE.md §2. Prompt *text* lives only in analyst/prompts/vN.md."""

    screener_model: str = "claude-sonnet-4-6"
    analyst_model: str = "claude-fable-5"
    analyst_effort: Literal["low", "medium", "high"] = "high"
    max_json_retries: int = 1


class TelegramConfig(_Strict):
    owner_timezone: str = "UTC"
    digest_hour_local: int = 8
    quiet_mode_on_no_setup: bool = True


class AppConfig(_Strict):
    """The whole non-secret runtime configuration."""

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
    data_quality: DataQualityConfig = DataQualityConfig()
    risk: RiskConfig = RiskConfig()
    ladder: LadderConfig = LadderConfig()
    management: ManagementConfig = ManagementConfig()
    llm: LLMConfig = LLMConfig()
    telegram: TelegramConfig = TelegramConfig()


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
