"""Config layering and spec-fidelity of the shipped defaults."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.core.config import AppConfig, Secrets, load_config


def test_repo_config_matches_risk_engine_spec(repo_config: AppConfig) -> None:
    """specs/RISK_ENGINE.md §1 defaults, verbatim."""
    risk = repo_config.risk
    assert risk.risk_per_trade_pct == Decimal("0.75")
    assert risk.max_open_risk_pct == Decimal("2.25")
    assert risk.max_leverage == 10
    assert risk.margin_budget_pct == Decimal("10")
    assert risk.daily_loss_limit_pct == Decimal("3.0")
    assert risk.max_positions == 4
    assert risk.min_rr_tp1 == Decimal("1.5")
    assert risk.max_entry_distance_pct == Decimal("3.0")
    assert risk.min_confidence == 60
    assert risk.stop_atr_min_multiple == Decimal("0.6")
    assert risk.stop_atr_max_multiple == Decimal("3.0")
    assert risk.liq_buffer_multiple == Decimal("2.0")


def test_ladder_weights_match_spec(repo_config: AppConfig) -> None:
    """specs/RISK_ENGINE.md §3 — 40/35/25, in that order."""
    assert repo_config.ladder.weights_pct == (Decimal("40"), Decimal("35"), Decimal("25"))
    assert sum(repo_config.ladder.weights_pct) == Decimal("100")


def test_money_values_are_decimal_not_float(repo_config: AppConfig) -> None:
    """CLAUDE.md: all money math is Decimal — no float artefacts from YAML."""
    assert isinstance(repo_config.risk.risk_per_trade_pct, Decimal)
    assert str(repo_config.risk.risk_per_trade_pct) == "0.75"


def test_watchlist_and_timeframes_match_specs(repo_config: AppConfig) -> None:
    assert repo_config.watchlist[:3] == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert len(repo_config.watchlist) == 10
    # Every tail is one more than its closed-candle requirement (the last candle
    # is in progress and gets dropped). Chart timeframes carry 320 closed rather
    # than the spec's 200 so EMA200 has enough valid points to span the
    # 120-candle chart window — see DATA_SOURCES §2.1.
    tails = {spec.timeframe: spec.candles for spec in repo_config.market_data.timeframes}
    closed = {tf: n - 1 for tf, n in tails.items()}

    assert closed == {"15m": 320, "1h": 320, "4h": 320, "1d": 100}
    chart_window = repo_config.charts.candle_window
    longest_ema = max(repo_config.charts.ema_periods)
    for timeframe in repo_config.charts.timeframes:
        # Valid EMA200 points across the window: closed - (period - 1).
        assert closed[timeframe] - (longest_ema - 1) >= chart_window, timeframe


def test_defaults_apply_when_yaml_omits_keys(tmp_path: Path) -> None:
    partial = tmp_path / "config.yaml"
    partial.write_text("risk:\n  max_leverage: 5\n", encoding="utf-8")

    config = load_config(partial)

    assert config.risk.max_leverage == 5  # from yaml
    assert config.risk.risk_per_trade_pct == Decimal("0.75")  # from defaults
    assert len(config.watchlist) == 10  # from defaults


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.yaml")
    assert config.risk.max_leverage == 10


def test_db_overrides_beat_yaml(tmp_path: Path) -> None:
    """ARCHITECTURE.md §2 precedence: DB > yaml > defaults."""
    partial = tmp_path / "config.yaml"
    partial.write_text("risk:\n  max_leverage: 5\n  max_positions: 2\n", encoding="utf-8")

    config = load_config(partial, db_overrides={"risk": {"max_leverage": 7}})

    assert config.risk.max_leverage == 7  # DB wins
    assert config.risk.max_positions == 2  # yaml survives the merge
    assert config.risk.risk_per_trade_pct == Decimal("0.75")  # defaults survive


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """A typo in config.yaml must fail loudly, not be silently ignored."""
    bad = tmp_path / "config.yaml"
    bad.write_text("risk:\n  max_leverag: 5\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(bad)


def test_non_mapping_yaml_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_config(bad)


def test_config_is_immutable(repo_config: AppConfig) -> None:
    with pytest.raises(ValidationError):
        repo_config.risk.max_leverage = 50  # type: ignore[misc]


def test_secrets_are_never_rendered(secrets: Secrets) -> None:
    """CLAUDE.md: secrets are never logged. Not via repr, not via str."""
    loaded = Secrets(
        _env_file=None,
        anthropic_api_key="sk-ant-super-secret",
        telegram_bot_token="123:telegram-secret",
    )
    assert "sk-ant-super-secret" not in repr(loaded)
    assert "sk-ant-super-secret" not in str(loaded)
    assert "telegram-secret" not in repr(loaded)
    assert loaded.anthropic_api_key is not None
    assert loaded.anthropic_api_key.get_secret_value() == "sk-ant-super-secret"
    assert secrets.anthropic_api_key is None


def test_telegram_allowlist_parsing() -> None:
    assert Secrets(_env_file=None, telegram_allowed_user_ids="").allowed_user_ids == ()
    parsed = Secrets(_env_file=None, telegram_allowed_user_ids="123, 456 ,789")
    assert parsed.allowed_user_ids == (123, 456, 789)


def test_json_logs_follow_environment() -> None:
    assert Secrets(_env_file=None, sentinel_env="prod").json_logs is True
    assert Secrets(_env_file=None, sentinel_env="dev").json_logs is False
