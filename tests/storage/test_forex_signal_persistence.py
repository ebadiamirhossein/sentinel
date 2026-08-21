"""A forex signal through the **real** repository into a **real** Postgres row.

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://…/sentinel_test \\
        .venv/bin/pytest tests/storage/test_forex_signal_persistence.py

Opt-in like every DB test here, and written **before** switch-on rather than after,
because journal/M10c_REPORT.md §13 named this as the sharpest of the four joins M10c's
own boundary leaves open: `signal_row` needed no change and the columns already exist,
so there is nothing schema-shaped to be *wrong* — but that is an argument, not a
measurement, and this project has a scar from taking one for the other.

**The Decimal assertions are the point, and they are HANDOFF §4 item 12 applied to forex
before it ships instead of after.** The capital defect was born in exactly this seam: a
value written as `Decimal("10000")` comes back from a `Numeric(38, 18)` column as
`Decimal('10000.000000000000000000')`. Equal in value, different in scale, and every
in-memory fixture in the suite is structurally blind to the difference. A `ForexPlan`
carries **twenty-two** Decimal fields — a pip of `0.0001`, a pip value of six decimals,
rungs, RR multiples, costs at four decimals — and `plan` is stored as JSONB with
``mode="json"``, so each one round-trips as a *string*. That is what makes it survivable,
and it is worth having a test that says so rather than a comment.

What is asserted, in order of what would hurt most if it were wrong:

1. the row exists and is stamped ``market='forex'``;
2. it rehydrates through ``plan_of`` as a **``ForexPlan``**, not as anything else;
3. every Decimal on it is **identical, scale included** — not merely equal;
4. a crypto plan written beside it is unaffected and comes back a ``TradePlan``;
5. the repository refuses a forex record handed to a crypto-scoped repository.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.bot.models import SignalRecord
from sentinel.bot.plans import plan_of
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market
from sentinel.fx.plan import ForexPlan
from sentinel.risk.models import TradePlan
from sentinel.storage.models import SignalRow
from sentinel.storage.repositories import SignalRepository
from tests.db_guard import TEST_DB_URL, requires_db
from tests.fx.forex_double import forex_plan
from tests.risk_double import approved_plan

OWNER = 7222549221


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await _clean(db)
        yield db
        await db.rollback()
        await _clean(db)
    await engine.dispose()


async def _clean(session: AsyncSession) -> None:
    await session.execute(delete(SignalRow).where(SignalRow.user_id == OWNER))
    await session.commit()


def decimals_of(payload: Any, path: str = "") -> dict[str, str]:
    """Every Decimal in a dumped plan, as the **string** it was written as.

    Strings rather than ``Decimal`` objects deliberately: comparing ``Decimal`` to
    ``Decimal`` is exactly the comparison that could not see the capital defect, because
    ``Decimal("10000") == Decimal("10000.000000000000000000")``. A string comparison sees
    the scale, which is the thing being asserted.
    """
    found: dict[str, str] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            found |= decimals_of(value, f"{path}.{key}" if path else key)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found |= decimals_of(value, f"{path}[{index}]")
    elif isinstance(payload, str) and _numeric(payload):
        found[path] = payload
    return found


def _numeric(value: str) -> bool:
    try:
        Decimal(value)
    except Exception:
        return False
    return True


async def write(session: AsyncSession, record: SignalRecord) -> SignalRecord:
    claimed = await SignalRepository(session, market=record.market).claim(record)
    assert claimed is not None, "the plan_id was already taken"
    await session.commit()
    return claimed


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


@requires_db
async def test_a_forex_signal_writes_a_real_row(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """Join 1 of §13, closed. The real repository, a real insert, a real row."""
    plan = forex_plan(repo_config)
    claimed = await write(
        session, SignalRecord(plan=plan, user_id=OWNER, number=0, market=Market.FOREX)
    )

    row = await session.get(SignalRow, claimed.signal_id)
    assert row is not None
    assert row.market == Market.FOREX.value
    assert row.symbol == "EURUSD"
    assert row.direction == "long"
    assert row.prompt_version == "fable_forex_v1"
    # Postgres assigns it, and nothing may show a signal number before it has one.
    assert claimed.number > 0


@requires_db
async def test_it_rehydrates_as_a_forex_plan_and_not_as_anything_else(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """FOREX.md §16.7 through Postgres rather than through a fake."""
    plan = forex_plan(repo_config)
    claimed = await write(
        session, SignalRecord(plan=plan, user_id=OWNER, number=0, market=Market.FOREX)
    )
    row = await session.get(SignalRow, claimed.signal_id)
    assert row is not None

    restored = plan_of(row.plan, Market(row.market))

    assert isinstance(restored, ForexPlan)
    assert restored == plan


@requires_db
async def test_every_decimal_survives_with_its_scale_intact(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """The assertion this whole file exists for (HANDOFF §4 item 12).

    ``signals.plan`` is JSONB and the plan is dumped with ``mode="json"``, so every
    Decimal crosses as a **string** and comes back as the same string. That is what
    makes it survivable — and the reason to assert it rather than reason about it is
    that the capital defect was born in the *other* kind of seam, a
    ``Numeric(38, 18)`` column, where the value survives and the scale does not.

    Twenty-two-odd figures, including the ones a wrong scale would make dangerous
    rather than ugly: the pip (``0.0001`` — a factor of ten here is spec defect D-a),
    the pip value at six decimals, and the cost total at four (``fx/rounding.cost_money``
    keeps them because at €200 cents-rounding a spread moves net RR by 0.01R).
    """
    plan = forex_plan(repo_config)
    claimed = await write(
        session, SignalRecord(plan=plan, user_id=OWNER, number=0, market=Market.FOREX)
    )
    row = await session.get(SignalRow, claimed.signal_id)
    assert row is not None

    written = decimals_of(plan.model_dump(mode="json"))
    read_back = decimals_of(row.plan)

    assert written, "the plan carries no Decimals — this test would be vacuous"
    assert len(written) > 20, f"expected the plan's full Decimal surface, got {len(written)}"
    assert read_back == written

    restored = plan_of(row.plan, Market(row.market))
    assert isinstance(restored, ForexPlan)
    for field in ("pip", "pip_value_eur", "risk_eur", "capital_eur", "eur_quote_rate"):
        original, returned = getattr(plan, field), getattr(restored, field)
        assert returned == original
        # ``str`` rather than ``==``: equality is what could not see the capital defect.
        assert str(returned) == str(original), f"{field} changed scale in the round trip"
    assert str(restored.costs.total_eur) == str(plan.costs.total_eur)


@requires_db
async def test_the_scale_check_would_notice_a_scale_change(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """Proof of teeth. A test that has never been seen to fail is indistinguishable
    from one that cannot, and this one's whole subject is a difference ``==`` hides."""
    plan = forex_plan(repo_config)
    original = decimals_of(plan.model_dump(mode="json"))
    widened = plan.model_copy(update={"capital_eur": Decimal("10000.000000000000000000")})

    changed = decimals_of(widened.model_dump(mode="json"))

    assert changed != original
    assert widened.capital_eur == plan.capital_eur  # equal in value...
    assert changed["capital_eur"] != original["capital_eur"]  # ...and not in scale


# --------------------------------------------------------------------------- #
# The two markets share a table and must not share anything else
# --------------------------------------------------------------------------- #


@requires_db
async def test_a_crypto_signal_beside_it_is_untouched_and_comes_back_a_trade_plan(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """One table, two plan shapes, and each row rehydrates as its own.

    The strongest form of the no-migration claim: not "the insert worked" but "both
    rows coexist and neither reads as the other".
    """
    crypto = await write(
        session,
        SignalRecord(plan=approved_plan(repo_config), user_id=OWNER, number=0),
    )
    forex = await write(
        session,
        SignalRecord(plan=forex_plan(repo_config), user_id=OWNER, number=0, market=Market.FOREX),
    )

    rows = (
        (await session.execute(select(SignalRow).where(SignalRow.user_id == OWNER))).scalars().all()
    )
    by_id = {row.id: row for row in rows}
    assert len(by_id) == 2

    restored_crypto = plan_of(by_id[crypto.signal_id].plan, Market.CRYPTO)
    restored_forex = plan_of(by_id[forex.signal_id].plan, Market.FOREX)

    assert isinstance(restored_crypto, TradePlan)
    assert isinstance(restored_forex, ForexPlan)
    assert by_id[crypto.signal_id].market == Market.CRYPTO.value
    assert by_id[forex.signal_id].market == Market.FOREX.value


@requires_db
async def test_a_market_scoped_read_never_returns_the_other_markets_row(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """``MarketScopedRepository``'s promise, with a forex row actually present.

    Until now every test of it ran against a table containing crypto rows only, which
    is the condition under which a missing filter also passes.
    """
    await write(session, SignalRecord(plan=approved_plan(repo_config), user_id=OWNER, number=0))
    await write(
        session,
        SignalRecord(plan=forex_plan(repo_config), user_id=OWNER, number=0, market=Market.FOREX),
    )

    crypto_open = await SignalRepository(session, market=Market.CRYPTO).open_symbols(user_id=OWNER)
    forex_open = await SignalRepository(session, market=Market.FOREX).open_symbols(user_id=OWNER)

    assert crypto_open == {"SOLUSDT"}
    assert forex_open == {"EURUSD"}


@requires_db
async def test_a_forex_record_handed_to_a_crypto_repository_is_refused(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """The write-side half of §16.7: a mis-stamped row never reaches the table."""
    record = SignalRecord(
        plan=forex_plan(repo_config), user_id=OWNER, number=0, market=Market.FOREX
    )

    with pytest.raises(ValueError, match="handed to a 'crypto' repository"):
        await SignalRepository(session, market=Market.CRYPTO).claim(record)


@requires_db
async def test_chart_params_survive_the_round_trip(
    session: AsyncSession, repo_config: AppConfig
) -> None:
    """Join 2 of §13's list, through Postgres.

    Decision #20's annotations were proved by dumping a model. This is the trip a
    stored chart actually takes — and the conditional serializer that omits the key
    when it is empty is exactly the sort of thing JSONB round-trips differently from
    a Python dict if anything is wrong.
    """
    params = ({"spec": {"symbol": "EURUSD"}, "annotations": [{"kind": "PRIOR_DAY_HIGH"}]},)
    claimed = await write(
        session,
        SignalRecord(
            plan=forex_plan(repo_config),
            user_id=OWNER,
            number=0,
            market=Market.FOREX,
            chart_params=params,
        ),
    )

    row = await session.get(SignalRow, claimed.signal_id)
    assert row is not None
    assert row.chart_params == [dict(params[0])]
    assert row.chart_params[0]["annotations"]
