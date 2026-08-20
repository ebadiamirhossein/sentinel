"""Config layering and spec-fidelity of the shipped defaults."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.core.config import AppConfig, Secrets, load_config
from sentinel.core.markets import Market


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
    crypto = repo_config.market(Market.CRYPTO)
    assert crypto.watchlist[:3] == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert len(crypto.watchlist) == 10
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
    assert len(config.market().watchlist) == 10  # from defaults


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


# --------------------------------------------------------------------------- #
# M10a — the markets block, and the config the server is actually running
# --------------------------------------------------------------------------- #

#: ``config.yaml`` exactly as it stood before M10a, frozen. This is the shape the
#: LIVE deployment loads: docs/DEPLOY.md §6 and §13 edit the server's copy in place
#: (``sed -i 's/^dry_run: true/dry_run: false/'``), so the running file has no
#: ``markets:`` block and ``dry_run: false``.
LEGACY_DEPLOYED = Path(__file__).resolve().parent / "fixtures" / "config_legacy_deployed.yaml"

#: What crypto's settings are today, field by field. Written out rather than
#: compared against ``config.yaml``, so a change to *either* file has to be a
#: deliberate edit here as well.
TODAYS_CRYPTO = {
    "enabled": True,
    "dry_run": False,
    "adapter": "crypto_binance",
    "watchlist_max_symbols": 15,
    "scan_interval_minutes": 60,
    "llm_daily_budget_usd": Decimal("10"),
    "llm_daily_warn_usd": Decimal("7"),
}


def test_the_deployed_legacy_config_still_loads() -> None:
    """The server runs a pre-M10a file. It must keep working, untouched.

    Not a courtesy to old files: the app applies migrations and starts before
    anybody edits ``config.yaml`` on the box, and ``ops/update.sh``'s ``git pull``
    does not overwrite a locally-modified tracked file. A build that could only read
    the new shape would fail to boot on the one machine that matters.
    """
    config = load_config(LEGACY_DEPLOYED)

    assert config.enabled_markets == (Market.CRYPTO,)
    assert not config.multi_market


@pytest.mark.parametrize("field,expected", sorted(TODAYS_CRYPTO.items()))
def test_the_legacy_shape_synthesises_todays_crypto_market(field: str, expected: object) -> None:
    """Field by field, so a failure names the value that moved."""
    assert getattr(load_config(LEGACY_DEPLOYED).market(Market.CRYPTO), field) == expected


def test_the_legacy_watchlist_survives_intact() -> None:
    assert load_config(LEGACY_DEPLOYED).market(Market.CRYPTO).watchlist == (
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


def test_the_new_config_and_the_legacy_one_agree_about_crypto(repo_config: AppConfig) -> None:
    """The equivalence the whole milestone rests on.

    ``config.yaml`` gained a ``markets:`` block; the frozen legacy file has none.
    They must describe the identical crypto market, or M10a changed the live
    system's behaviour by editing a config file.
    """
    assert repo_config.market(Market.CRYPTO) == load_config(LEGACY_DEPLOYED).market(Market.CRYPTO)


@pytest.mark.parametrize("field", ["llm_daily_budget_usd", "llm_daily_warn_usd"])
def test_the_money_values_render_identically_too(repo_config: AppConfig, field: str) -> None:
    """Equal is not enough for a ``Decimal`` that reaches a card.

    ``Decimal("10.0") == Decimal("10")`` is ``True`` and ``str()`` of them differs —
    so the equality test above passes while ``/status`` renders "$10.0" where it used
    to say "$10". Found by the ``/status`` golden during M10a, and pinned here as
    well so the next person changing config.yaml is told which of the two rules they
    broke. Write these as ``10``, never ``10.00``: YAML parses the latter as a float.
    """
    new = getattr(repo_config.market(Market.CRYPTO), field)
    old = getattr(load_config(LEGACY_DEPLOYED).market(Market.CRYPTO), field)

    assert str(new) == str(old)


def test_forex_ships_disabled_and_unimplemented(repo_config: AppConfig) -> None:
    """M10a adds the dimension and no forex code. The config says so out loud."""
    forex = repo_config.market(Market.FOREX)

    assert forex.enabled is False
    assert forex.dry_run is True
    assert forex.adapter == "forex_saxo"
    assert forex.watchlist == ("EURUSD", "GBPUSD", "USDJPY")
    assert Market.FOREX not in repo_config.enabled_markets


def test_the_sub_budgets_deliberately_exceed_the_global_ceiling(
    repo_config: AppConfig,
) -> None:
    """10 + 4 against 11, on purpose (M10a Step 4).

    Budgets that summed to the ceiling would let a quiet market reserve money a busy
    one could use. Asserted rather than left as a comment, because it looks exactly
    like an arithmetic mistake and somebody would "fix" it.
    """
    total = sum(repo_config.market(market).llm_daily_budget_usd for market in repo_config.markets)

    assert total > repo_config.llm_daily_budget_global_usd
    assert repo_config.llm_daily_budget_global_usd == Decimal("11")


def test_a_config_carrying_both_shapes_is_refused(tmp_path: Path) -> None:
    """Two places to set ``dry_run`` is one place for it to be wrong — silently, and
    in the direction that publishes real cards during a rehearsal."""
    both = tmp_path / "config.yaml"
    both.write_text("dry_run: true\nmarkets:\n  crypto:\n    enabled: true\n", encoding="utf-8")

    # ``ValueError`` and not ``ValidationError``: ``load_config`` normalises before
    # it validates, so the refusal happens in ``normalise_markets``. Pydantic's
    # ``ValidationError`` is a ``ValueError`` too, so this also covers the model path.
    with pytest.raises(ValueError, match="two places to set the same value"):
        load_config(both)


def test_an_unknown_market_name_is_refused(tmp_path: Path) -> None:
    """A typo must fail at load, not silently configure nothing."""
    typo = tmp_path / "config.yaml"
    typo.write_text("markets:\n  crytpo:\n    enabled: true\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(typo)


def test_asking_for_an_unconfigured_market_raises(tmp_path: Path) -> None:
    """Not an empty default. A missing market is a wiring mistake, and inventing an
    empty watchlist for it would turn that into a silently quiet cycle."""
    crypto_only = tmp_path / "config.yaml"
    crypto_only.write_text("markets:\n  crypto:\n    enabled: true\n", encoding="utf-8")

    with pytest.raises(KeyError, match="forex"):
        load_config(crypto_only).market(Market.FOREX)
