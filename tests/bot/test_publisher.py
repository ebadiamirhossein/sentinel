"""Idempotent posting — specs/TELEGRAM_UX.md §6, the headline M6 requirement.

"All messages idempotent (message ids stored; restarts never double-post)."

The publisher is driven against a ``FakeBot`` and an in-memory store that models
the two unique constraints the guarantee actually rests on. No aiogram session is
constructed anywhere in this file — the autouse ``no_network`` guard would fail
the run if one were, which is what proves the suite makes no live Telegram calls.
``test_persistence.py`` replays the same scenarios against real Postgres.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from sentinel.bot.models import MessageKind
from sentinel.bot.publisher import SignalPublisher
from sentinel.charts.models import ChartImage
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.models import TradePlan
from tests.bot_double import FakeBot, FakeDatabase, FakeMessageStore, FakeSignalStore, FakeStore
from tests.market_double import chart_album

CHAT_ID = 4242


def publisher(
    database: FakeDatabase, bot: FakeBot, config: AppConfig, tz: ZoneInfo, clock: FrozenClock
) -> SignalPublisher:
    return SignalPublisher(
        database,  # type: ignore[arg-type]
        bot,
        chat_ids=(CHAT_ID,),
        telegram=config.telegram,
        tz=tz,
        clock=clock,
        signals=FakeSignalStore,
        messages=FakeMessageStore,
    )


async def test_publishing_sends_an_album_then_a_card_with_buttons(
    fake_database: FakeDatabase,
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    result = await publisher(fake_database, fake_bot, bot_config, tz, clock).publish(
        plan, tuple(chart_album("SOLUSDT"))
    )

    assert result.published is True
    assert [call.method for call in fake_bot.calls] == ["send_media_group", "send_message"]

    card = fake_bot.of("send_message")[0]
    assert card.kwargs["chat_id"] == CHAT_ID
    assert card.kwargs["reply_markup"] is not None, "the card must carry the §2 buttons"
    assert card.kwargs["reply_to_message_id"] == fake_bot.of("send_media_group")[0].message_id
    assert "🟢 LONG — SOLUSDT" in card.kwargs["text"]


async def test_only_the_spec_s_chart_timeframes_are_attached(
    fake_database: FakeDatabase,
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """§1 attaches 1h and 4h. The analyst still gets all three (15m included)."""
    await publisher(fake_database, fake_bot, bot_config, tz, clock).publish(
        plan, tuple(chart_album("SOLUSDT"))
    )
    media = fake_bot.of("send_media_group")[0].kwargs["media"]
    filenames = [item.media.filename for item in media]
    assert filenames == ["SOLUSDT_1h.png", "SOLUSDT_4h.png"]


async def test_publishing_the_same_plan_twice_posts_once(
    fake_database: FakeDatabase,
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """A duplicated cycle, a retry, a double-click on the demo tool — all one card."""
    pub = publisher(fake_database, fake_bot, bot_config, tz, clock)
    first = await pub.publish(plan)
    second = await pub.publish(plan)

    assert first.published is True
    assert second.published is False
    assert second.reason == "plan already published"
    assert len(fake_bot.of("send_message")) == 1


async def test_a_restart_never_double_posts(
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """The database is what survives a restart, so the guarantee has to live there.

    A brand-new publisher over a brand-new "process" — the same store, a fresh
    bot — must send nothing for a plan already delivered.
    """
    store = FakeStore()
    await publisher(FakeDatabase(store), fake_bot, bot_config, tz, clock).publish(plan)
    assert len(fake_bot.of("send_message")) == 1

    after_restart = FakeBot()
    result = await publisher(FakeDatabase(store), after_restart, bot_config, tz, clock).publish(
        plan
    )

    assert result.published is False
    assert after_restart.calls == []


async def test_a_claim_that_never_confirmed_is_not_re_sent(
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """The crash window, decided deliberately (§6 and the publisher's docstring).

    A process that died between the send and the confirmation leaves a PENDING
    claim. It is never retried: a duplicated signal card is worse than a missing
    one the owner can ask for again, and /status surfaces the stuck row so the gap
    is visible rather than silent.
    """
    store = FakeStore()
    pub = publisher(FakeDatabase(store), fake_bot, bot_config, tz, clock)
    result = await pub.publish(plan)
    assert result.record is not None

    # Simulate the crash: the claim exists, the confirmation never landed.
    # The card claims the empty ``event_key``; M7 widened the key so a signal's
    # thread can carry many updates without colliding with the card itself.
    key = (result.record.signal_id, MessageKind.CARD.value, CHAT_ID, "")
    store.messages[key].status = "PENDING"
    store.messages[key].message_id = None

    after_restart = FakeBot()
    await publisher(FakeDatabase(store), after_restart, bot_config, tz, clock).publish(plan)
    assert after_restart.calls == []


async def test_the_claim_is_committed_before_anything_is_sent(
    fake_database: FakeDatabase,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """The ordering *is* the guarantee, so assert it rather than trusting the prose.

    If the send happened first, a crash before the commit would leave no claim and
    the next start would post a second card.
    """
    committed_at_send: list[int] = []
    store = FakeStore()

    class RecordingBot(FakeBot):
        async def send_message(self, **kwargs: Any) -> Any:
            committed_at_send.append(store.committed)
            return await super().send_message(**kwargs)

    await publisher(FakeDatabase(store), RecordingBot(), bot_config, tz, clock).publish(plan)
    assert committed_at_send and committed_at_send[0] >= 2, (
        "the signal row and the message claim must both be committed before the send"
    )


async def test_a_failed_send_is_recorded_and_does_not_raise(
    bot_config: AppConfig, tz: ZoneInfo, clock: FrozenClock, plan: TradePlan
) -> None:
    """A Telegram outage must not take the cycle down with it (ARCHITECTURE §6)."""
    store = FakeStore()
    bot = FakeBot(fail={"send_message": RuntimeError("telegram is down")})

    result = await publisher(FakeDatabase(store), bot, bot_config, tz, clock).publish(plan)

    assert result.published is False
    key = (next(iter(store.signals)), MessageKind.CARD.value, CHAT_ID, "")
    assert store.messages[key].status == "FAILED"
    assert "telegram is down" in (store.messages[key].error or "")


async def test_a_plan_with_no_charts_still_posts_the_card(
    fake_database: FakeDatabase,
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """Chart rendering failing must not cost the owner the signal itself."""
    result = await publisher(fake_database, fake_bot, bot_config, tz, clock).publish(plan, ())
    assert result.published is True
    assert fake_bot.of("send_media_group") == []
    assert len(fake_bot.of("send_message")) == 1


async def test_charts_are_sent_by_reference_to_their_rendered_bytes(
    fake_database: FakeDatabase,
    fake_bot: FakeBot,
    bot_config: AppConfig,
    tz: ZoneInfo,
    clock: FrozenClock,
    plan: TradePlan,
) -> None:
    """The signal record keeps ChartRenderParams, per M3 §7 — no separate table."""
    charts: list[ChartImage] = chart_album("SOLUSDT")
    result = await publisher(fake_database, fake_bot, bot_config, tz, clock).publish(
        plan, tuple(charts)
    )
    assert result.record is not None
    assert len(result.record.chart_params) == len(charts)
    assert all("image_sha256" in params for params in result.record.chart_params)
