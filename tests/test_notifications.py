import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.actions import apply_item_action
from app.services.attention_ranking import AttentionRankingService
from app.services.notifications import (
    ATTENTION_POLICIES,
    DAILY_DIGEST,
    MIN_PROACTIVE_ATTENTION_SCORE,
    PROACTIVE_ATTENTION,
    SNOOZE_RESURFACE,
    ReminderWorker,
    _local_day_window,
    attention_policy,
    get_notification_settings,
    settings_for,
    update_notification_settings,
)
from app.storage.models import Event, Item, ItemSource, Reminder, User


class FakeBot:
    def __init__(self, fail=False, failures=0):
        self.fail = fail
        self.failures = failures
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail or self.failures:
            self.failures = max(0, self.failures - 1)
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text, kwargs))


async def make_ready_item(
    session_factory,
    *,
    state=ItemState.ACTIVE,
    snoozed_until=None,
    attention_enabled=False,
):
    async with session_factory() as session:
        user = User(
            telegram_user_id=42,
            telegram_chat_id=42,
            timezone="Europe/Moscow",
            settings_json={"attention_enabled": attention_enabled},
        )
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            processing_status=ProcessingStatus.READY,
            state=state,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="item",
            item_type=ItemType.ACTION,
            title="Сделать задачу",
            priority_score=80,
            snoozed_until=snoozed_until,
        )
        session.add(item)
        await session.commit()
        return user.id, item.id


async def add_ready_item(session_factory, user_id: int, *, title="Another task", priority=80):
    async with session_factory() as session:
        item = Item(
            user_id=user_id,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note=title,
            item_type=ItemType.ACTION,
            title=title,
            priority_score=priority,
        )
        session.add(item)
        await session.commit()
        return item.id


async def test_timezone_digest_is_once_per_local_day(session_factory):
    _, _ = await make_ready_item(session_factory)
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    now = datetime(2026, 9, 14, 6, 30)  # 09:30 in Europe/Moscow

    assert await worker.process_once(now) == 1
    assert await worker.process_once(now + timedelta(minutes=1)) == 0
    assert len(bot.messages) == 1
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.status == "SENT"


async def test_digest_claim_becomes_successful_only_after_telegram_accepts(session_factory):
    await make_ready_item(session_factory)
    now = datetime(2026, 9, 14, 6, 30)

    class InspectClaimBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            async with session_factory() as session:
                reminder = await session.scalar(
                    select(Reminder).where(Reminder.type == DAILY_DIGEST)
                )
                assert reminder.status == "CLAIMED"
                assert reminder.sent_at is None
            await super().send_message(chat_id, text, **kwargs)

    assert await ReminderWorker(session_factory, InspectClaimBot()).process_once(now) == 1
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.status == "SENT"
        assert reminder.sent_at == now


async def test_digest_due_during_quiet_hours_is_deferred_to_morning(session_factory):
    await make_ready_item(session_factory)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_time="23:00",
        quiet_hours_start="22:30",
        quiet_hours_end="08:00",
    )
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    assert await worker.process_once(datetime(2026, 9, 14, 20, 0)) == 0
    assert await worker.process_once(datetime(2026, 9, 15, 4, 59)) == 0
    assert await worker.process_once(datetime(2026, 9, 15, 6, 0)) == 1
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.payload_json["local_date"] == "2026-09-14"


async def test_digest_due_before_overnight_quiet_is_deferred_without_losing_date(session_factory):
    user_id, _ = await make_ready_item(session_factory)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        user.daily_digest_enabled_at = datetime(2026, 9, 14, 10, 0)
        await session.commit()
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_time="21:00",
        quiet_hours_start="22:30",
        quiet_hours_end="08:00",
    )
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    # `process_once` receives UTC; these instants are 23:00 local and then
    # 08:00/08:01 local in Europe/Moscow. The due moment at 21:00 was missed.
    assert await worker.process_once(datetime(2026, 9, 14, 20, 0)) == 0
    assert await worker.process_once(datetime(2026, 9, 15, 5, 0)) == 1
    assert await worker.process_once(datetime(2026, 9, 15, 5, 1)) == 0
    assert len(bot.messages) == 1
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.payload_json["local_date"] == "2026-09-14"


async def test_digest_after_midnight_restart_is_deferred_from_previous_local_day(session_factory):
    user_id, _ = await make_ready_item(session_factory)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        user.created_at = datetime(2026, 9, 14, 10, 0)
        user.daily_digest_enabled_at = datetime(2026, 9, 14, 10, 0)
        # A later unrelated update must not disable recovery.
        user.updated_at = datetime(2026, 9, 14, 20, 0)
        await session.commit()
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_time="21:00",
        quiet_hours_start="22:30",
        quiet_hours_end="08:00",
    )
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    # First poll after downtime is 01:00 local; the previous day's 21:00
    # digest is due but delivery remains blocked by quiet hours.
    assert await worker.process_once(datetime(2026, 9, 14, 22, 0)) == 0
    assert await worker.process_once(datetime(2026, 9, 15, 5, 0)) == 1
    assert await worker.process_once(datetime(2026, 9, 15, 5, 1)) == 0
    assert len(bot.messages) == 1


async def test_new_user_after_midnight_has_no_stale_digest(session_factory):
    user_id, _ = await make_ready_item(session_factory)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        # User was created at 01:00 local on the simulated day, after the
        # previous day's 09:00 schedule had already passed.
        user.created_at = datetime(2026, 9, 14, 22, 0)
        user.updated_at = datetime(2026, 9, 14, 22, 0)
        user.daily_digest_enabled_at = datetime(2026, 9, 14, 22, 0)
        await session.commit()
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(datetime(2026, 9, 14, 22, 0)) == 0
    assert await worker.process_once(datetime(2026, 9, 15, 5, 0)) == 0
    assert await worker.process_once(datetime(2026, 9, 15, 6, 0)) == 1
    assert len(bot.messages) == 1
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.payload_json["local_date"] == "2026-09-15"


async def test_digest_does_not_synthetic_catch_up_before_configured_time(session_factory):
    await make_ready_item(session_factory)
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    # 08:30 local is before the default 09:00 digest and has no deferred row.
    assert await worker.process_once(datetime(2026, 9, 14, 5, 30)) == 0
    assert await worker.process_once(datetime(2026, 9, 14, 6, 0)) == 1
    assert len(bot.messages) == 1
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.payload_json["local_date"] == "2026-09-14"


def test_clock_parser_rejects_offsets_and_seconds():
    from app.services.notifications import parse_clock

    with pytest.raises(ValueError):
        parse_clock("09:00Z")
    with pytest.raises(ValueError):
        parse_clock("09:00+03:00")
    with pytest.raises(ValueError):
        parse_clock("09:00:01")


async def test_digest_does_not_duplicate_after_worker_restart(session_factory):
    await make_ready_item(session_factory)
    now = datetime(2026, 9, 14, 6, 30)
    first_bot = FakeBot()
    assert await ReminderWorker(session_factory, first_bot).process_once(now) == 1
    second_bot = FakeBot()
    assert await ReminderWorker(session_factory, second_bot).process_once(now) == 0
    assert second_bot.messages == []


async def test_concurrent_workers_claim_one_digest(session_factory):
    await make_ready_item(session_factory)
    now = datetime(2026, 9, 14, 6, 30)
    first_bot, second_bot = FakeBot(), FakeBot()
    results = await asyncio.gather(
        ReminderWorker(session_factory, first_bot).process_once(now),
        ReminderWorker(session_factory, second_bot).process_once(now),
    )
    assert sorted(results) == [0, 1]
    assert len(first_bot.messages) + len(second_bot.messages) == 1


async def test_timezone_change_same_local_day_does_not_duplicate_digest(session_factory):
    await make_ready_item(session_factory)
    now = datetime(2026, 9, 14, 10, 0)  # 13:00 Europe/Moscow
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    assert await worker.process_once(now) == 1
    await update_notification_settings(session_factory, 42, timezone="UTC")
    assert await worker.process_once(now) == 0


async def test_concurrent_disjoint_settings_updates_are_merged(session_factory):
    await make_ready_item(session_factory)
    await asyncio.gather(
        update_notification_settings(session_factory, 42, daily_digest_time="08:30"),
        update_notification_settings(session_factory, 42, quiet_hours_start="20:00"),
        update_notification_settings(
            session_factory, 42, attention_enabled=True, attention_intensity=4
        ),
    )
    current = await get_notification_settings(session_factory, 42)
    assert current[1]["daily_digest_time"] == "08:30"
    assert current[1]["quiet_hours_start"] == "20:00"
    assert current[1]["attention_enabled"] is True
    assert current[1]["attention_intensity"] == 4


async def test_transient_notification_failure_retries_before_terminal_failure(session_factory):
    await make_ready_item(session_factory)
    bot = FakeBot(failures=2)
    worker = ReminderWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_once(datetime(2026, 9, 14, 6, 30)) == 1
    assert len(bot.messages) == 1


async def test_snoozed_item_becomes_active_and_notifies(session_factory):
    until = datetime(2026, 9, 14, 6, 0)
    _, item_id = await make_ready_item(session_factory, state=ItemState.ACTIVE, snoozed_until=None)
    await apply_item_action(session_factory, 42, item_id, "snooze", until)
    await update_notification_settings(session_factory, 42, daily_digest_enabled=False)

    class InspectClaimBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            async with session_factory() as session:
                reminder = await session.scalar(
                    select(Reminder).where(
                        Reminder.item_id == item_id,
                        Reminder.type == SNOOZE_RESURFACE,
                    )
                )
                assert reminder.status == "CLAIMED"
                assert reminder.sent_at is None
            await super().send_message(chat_id, text, **kwargs)

    bot = InspectClaimBot()
    assert await ReminderWorker(session_factory, bot).process_once(datetime(2026, 9, 14, 7, 0)) == 1
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        reminder = await session.scalar(select(Reminder).where(Reminder.item_id == item_id))
        assert item.state is ItemState.ACTIVE
        assert item.snoozed_until is None
        assert reminder.status == "SENT"
    assert "Вернулся" in bot.messages[0][1]


async def test_failed_snooze_send_does_not_start_proactive_minimum_gap(session_factory):
    now = datetime(2026, 9, 14, 6, 30)
    _, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await apply_item_action(session_factory, 42, item_id, "snooze", now - timedelta(minutes=1))
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )

    class FailSnoozeThenSucceed(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if text.startswith("⏰ Вернулся"):
                raise RuntimeError("Telegram unavailable for snooze")
            await super().send_message(chat_id, text, **kwargs)

    bot = FailSnoozeThenSucceed()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    assert len(bot.messages) == 1
    assert "⏳ Вернём это в фокус" in bot.messages[0][1]
    async with session_factory() as session:
        snooze = await session.scalar(
            select(Reminder).where(Reminder.item_id == item_id, Reminder.type == SNOOZE_RESURFACE)
        )
        proactive = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert snooze.status == "FAILED"
        assert snooze.sent_at is None
        assert proactive.status == "SENT"


async def test_snooze_resurfacing_respects_quiet_hours(session_factory):
    until = datetime(2026, 9, 14, 22, 0)
    _, item_id = await make_ready_item(session_factory)
    await apply_item_action(session_factory, 42, item_id, "snooze", until)
    await update_notification_settings(session_factory, 42, daily_digest_enabled=False)
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    assert await worker.process_once(datetime(2026, 9, 14, 23, 0)) == 0
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        reminder = await session.scalar(select(Reminder).where(Reminder.item_id == item_id))
        assert item.state is ItemState.SNOOZED
        assert reminder.status == "PENDING"
    assert await worker.process_once(datetime(2026, 9, 15, 6, 1)) == 1
    assert len(bot.messages) == 1


async def test_proactive_attention_waits_until_quiet_hours_end(session_factory):
    await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(datetime(2026, 9, 14, 23, 0)) == 0
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(Reminder.type == PROACTIVE_ATTENTION)
            )
            == 0
        )
    assert bot.messages == []
    assert await worker.process_once(datetime(2026, 9, 15, 5, 0)) == 1
    assert len(bot.messages) == 1


async def test_notification_failure_is_recorded(session_factory):
    await make_ready_item(session_factory)
    bot = FakeBot(fail=True)
    now = datetime(2026, 9, 14, 6, 30)
    assert await ReminderWorker(session_factory, bot).process_once(now) == 0
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.status == "FAILED"


async def test_done_cancels_pending_snooze_notification(session_factory):
    until = datetime(2026, 9, 14, 6, 0)
    _, item_id = await make_ready_item(session_factory)
    await apply_item_action(session_factory, 42, item_id, "snooze", until)
    await apply_item_action(session_factory, 42, item_id, "done")
    bot = FakeBot()
    assert (
        await ReminderWorker(session_factory, bot).process_once(datetime(2026, 9, 14, 7, 0)) == 1
    )  # only the daily digest; the stale snooze is cancelled
    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.item_id == item_id, Reminder.type == SNOOZE_RESURFACE)
        )
        assert reminder.status == "CANCELLED"


async def test_notification_settings_validate_and_persist(session_factory):
    await make_ready_item(session_factory)
    updated = await update_notification_settings(
        session_factory,
        42,
        timezone="UTC",
        daily_digest_enabled=False,
        daily_digest_time="10:15",
        quiet_hours_start="21:00",
        quiet_hours_end="07:00",
    )
    assert updated[0].timezone == "UTC"
    assert updated[1]["daily_digest_enabled"] is False
    current = await get_notification_settings(session_factory, 42)
    assert current[1]["daily_digest_time"] == "10:15"


def test_attention_policy_table_and_validation_are_exact():
    expected = {
        1: ("Calm", 1, timedelta(hours=8), timedelta(hours=72)),
        2: ("Light", 2, timedelta(hours=5), timedelta(hours=48)),
        3: ("Normal", 3, timedelta(hours=3), timedelta(hours=30)),
        4: ("Active", 4, timedelta(hours=2), timedelta(hours=20)),
        5: ("Aggressive", 6, timedelta(minutes=90), timedelta(hours=12)),
    }
    assert {
        level: (
            policy.label,
            policy.daily_cap,
            policy.minimum_gap,
            policy.same_item_cooldown,
        )
        for level, policy in ATTENTION_POLICIES.items()
    } == expected
    with pytest.raises(TypeError):
        ATTENTION_POLICIES[6] = ATTENTION_POLICIES[5]
    for value in (0, 6, "high", True):
        with pytest.raises(ValueError):
            attention_policy(value)
    assert MIN_PROACTIVE_ATTENTION_SCORE == 60


async def test_attention_defaults_and_settings_validation(session_factory):
    async with session_factory() as session:
        session.add(User(telegram_user_id=42, telegram_chat_id=42, timezone="Europe/Moscow"))
        await session.commit()
    current = await get_notification_settings(session_factory, 42)
    assert current[1]["attention_enabled"] is True
    assert current[1]["attention_intensity"] == 3

    updated = await update_notification_settings(
        session_factory, 42, attention_enabled=False, attention_intensity=5
    )
    assert updated[1]["attention_enabled"] is False
    assert updated[1]["attention_intensity"] == 5
    for value in (0, 6, "high", True):
        with pytest.raises(ValueError):
            await update_notification_settings(session_factory, 42, attention_intensity=value)
    with pytest.raises(ValueError):
        await update_notification_settings(session_factory, 42, attention_enabled=1)

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user.settings_json == {
            "attention_enabled": False,
            "attention_intensity": 5,
        }
    assert settings_for(User(settings_json={}))["attention_enabled"] is True


async def test_proactive_delivery_persists_reminder_and_exposure(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    async with session_factory() as session:
        source = ItemSource(
            item_id=item_id,
            source_index=0,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=example",
            extraction_status="READY",
        )
        session.add(source)
        await session.commit()
        source_id = source.id
    bot = FakeBot()
    now = datetime(2026, 9, 14, 6, 30)

    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    assert len(bot.messages) == 1
    _, message, kwargs = bot.messages[0]
    assert "Сделать задачу" in message
    assert "Почему сейчас:" in message
    assert "Внимание:" in message
    assert "Приоритет:" in message
    assert "Интерес:" in message
    assert "Сохранён:" in message
    callbacks = {
        button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row
    }
    urls = {
        button.url for row in kwargs["reply_markup"].inline_keyboard for button in row if button.url
    }
    assert f"item:done:{item_id}" in callbacks
    assert f"item:later:{item_id}" in callbacks
    assert f"item:archive:{item_id}" in callbacks
    assert f"item:video:{item_id}:{source_id}" in callbacks
    assert "https://www.youtube.com/watch?v=example" in urls

    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        event = await session.scalar(select(Event).where(Event.event_type == "ATTENTION_SHOWN"))
        assert reminder.user_id == user_id
        assert reminder.item_id == item_id
        assert reminder.status == "SENT"
        assert reminder.sent_at == now
        assert reminder.payload_json["policy_level"] == 3
        assert reminder.payload_json["attention_score"] >= MIN_PROACTIVE_ATTENTION_SCORE
        assert len(str(reminder.payload_json)) < 500
        assert event.item_id == item_id
        assert event.payload_json == {
            "source": "proactive_attention",
            "reminder_id": reminder.id,
        }
        assert event.idempotency_key == f"reminder:{reminder.id}:attention_shown"
        assert event.created_at == now
        ranked = await AttentionRankingService().list_candidates(session, user_id, now=now)
        assert ranked[0][0].id == item_id
        assert ranked[0][1].recent_show_penalty == -25


async def test_level_five_sends_at_most_one_and_minimum_gap_blocks_next_poll(session_factory):
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_intensity=5,
        attention_enabled=True,
    )
    for index in range(5):
        await add_ready_item(session_factory, user_id, title=f"Task {index}")
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    now = datetime(2026, 9, 14, 6, 30)

    assert await worker.process_once(now) == 1
    assert await worker.process_once(now + timedelta(minutes=1)) == 0
    assert len(bot.messages) == 1
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.type == PROACTIVE_ATTENTION,
                Reminder.status == "SENT",
            )
        )
        assert count == 1


async def test_digest_consumes_budget_after_its_gap_has_elapsed(session_factory):
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_intensity=1,
        attention_enabled=True,
    )
    now = datetime(2026, 9, 14, 15, 0)
    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=datetime(2026, 9, 14),
                status="SENT",
                sent_at=now - timedelta(hours=9),
            )
        )
        await session.commit()

    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(Reminder.type == PROACTIVE_ATTENTION)
            )
            == 0
        )


async def test_budget_day_uses_current_timezone_without_rewriting_history(session_factory):
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        attention_intensity=1,
        timezone="America/Los_Angeles",
    )
    sent_at = datetime(2026, 9, 15, 6, 0)
    now = datetime(2026, 9, 15, 15, 0)  # 08:00 in Los Angeles
    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=datetime(2026, 9, 14),
                status="SENT",
                sent_at=sent_at,
            )
        )
        await session.commit()

    worker = ReminderWorker(session_factory, FakeBot())
    async with session_factory() as session:
        user = await session.get(User, user_id)
        _, _, local_date, blocked = await worker._attention_gate(session, user, now)
        assert local_date.isoformat() == "2026-09-15"
        assert blocked is None

    await update_notification_settings(session_factory, 42, timezone="UTC")
    async with session_factory() as session:
        user = await session.get(User, user_id)
        _, _, local_date, blocked = await worker._attention_gate(session, user, now)
        assert local_date.isoformat() == "2026-09-15"
        assert blocked == "daily_cap"
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.sent_at == sent_at


async def test_manual_attention_exposure_does_not_consume_notification_budget(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    async with session_factory() as session:
        session.add(
            Event(
                user_id=user_id,
                item_id=item_id,
                event_type="ATTENTION_SHOWN",
                created_at=now - timedelta(days=1),
            )
        )
        await session.commit()

    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status == "SENT",
                )
            )
            == 1
        )


async def test_snooze_uses_gap_but_not_daily_budget(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    now = datetime(2026, 9, 14, 6, 30)
    await apply_item_action(session_factory, 42, item_id, "snooze", now - timedelta(minutes=1))
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(now) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(Reminder.type == PROACTIVE_ATTENTION)
            )
            == 0
        )
        snooze = await session.scalar(
            select(Reminder).where(Reminder.item_id == item_id, Reminder.type == SNOOZE_RESURFACE)
        )
        assert snooze.status == "SENT"

    assert await worker.process_once(now + timedelta(hours=3)) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.type == PROACTIVE_ATTENTION,
                    Reminder.status == "SENT",
                )
            )
            == 1
        )


async def test_minimum_gap_exact_boundary_uses_next_ranked_item(session_factory):
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    await add_ready_item(session_factory, user_id, title="Second task")
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    now = datetime(2026, 9, 14, 6, 30)

    assert await worker.process_once(now) == 1
    assert await worker.process_once(now + timedelta(hours=2, minutes=59, seconds=59)) == 0
    assert await worker.process_once(now + timedelta(hours=3)) == 1
    assert len(bot.messages) == 2


async def test_same_item_cooldown_exact_boundary_and_restart(session_factory):
    await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1

    assert (
        await ReminderWorker(session_factory, FakeBot()).process_once(
            now + timedelta(hours=29, minutes=59, seconds=59)
        )
        == 0
    )
    recovered_bot = FakeBot()
    assert (
        await ReminderWorker(session_factory, recovered_bot).process_once(now + timedelta(hours=30))
        == 1
    )
    assert len(recovered_bot.messages) == 1


async def test_unfinalized_digest_claim_does_not_consume_budget(session_factory):
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        attention_intensity=1,
    )
    now = datetime(2026, 9, 14, 15, 0)
    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=user_id,
                item_id=None,
                type=DAILY_DIGEST,
                scheduled_at=datetime(2026, 9, 14),
                status="CLAIMED",
            )
        )
        await session.commit()

    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    assert "Сделать задачу" in bot.messages[0][1]
    async with session_factory() as session:
        sent = await session.scalar(
            select(Reminder).where(
                Reminder.type == PROACTIVE_ATTENTION,
                Reminder.status == "SENT",
            )
        )
        assert sent is not None


async def test_cooldown_top_five_does_not_hide_sixth_candidate(session_factory):
    user_id, first_item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_intensity=5,
        attention_enabled=True,
    )
    item_ids = [first_item_id]
    for index in range(5):
        item_ids.append(await add_ready_item(session_factory, user_id, title=f"Task {index}"))
    now = datetime(2026, 9, 14, 6, 30)
    async with session_factory() as session:
        for item_id in item_ids[:5]:
            session.add(
                Reminder(
                    user_id=user_id,
                    item_id=item_id,
                    type=PROACTIVE_ATTENTION,
                    scheduled_at=now - timedelta(hours=2),
                    status="SENT",
                    sent_at=now - timedelta(hours=2),
                )
            )
        await session.commit()

    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    async with session_factory() as session:
        sent = await session.scalar(
            select(Reminder).where(
                Reminder.type == PROACTIVE_ATTENTION,
                Reminder.status == "SENT",
                Reminder.sent_at == now,
            )
        )
        assert sent.item_id == item_ids[5]


async def test_below_threshold_does_not_create_reminder_then_score_sixty_qualifies(
    session_factory,
):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        item.priority_score = 59
        await session.commit()
    now = datetime(2026, 9, 14, 6, 30)
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(now) == 0
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(Reminder.type == PROACTIVE_ATTENTION)
            )
            == 0
        )
        item = await session.get(Item, item_id)
        item.priority_score = 60
        await session.commit()
    assert await worker.process_once(now + timedelta(seconds=1)) == 1


def test_budget_window_uses_local_calendar_day_across_dst():
    local_date, start, end = _local_day_window(
        datetime(2026, 3, 29, 12), ZoneInfo("Europe/Helsinki")
    )
    assert local_date.isoformat() == "2026-03-29"
    assert end - start == timedelta(hours=23)


async def test_stale_open_claim_recovers_with_current_rank_after_restart(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    async with session_factory() as session:
        reminder = Reminder(
            user_id=user_id,
            item_id=item_id,
            type=PROACTIVE_ATTENTION,
            scheduled_at=now - timedelta(minutes=6),
            created_at=now - timedelta(minutes=6),
            status="CLAIMED",
            payload_json={"attention_score": 99},
        )
        session.add(reminder)
        await session.commit()
        reminder_id = reminder.id

    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        proactive_rows = (
            await session.scalars(select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION))
        ).all()
        assert len(proactive_rows) == 1
        assert reminder.status == "SENT"
        assert reminder.sent_at == now
        assert reminder.payload_json["attention_score"] == 80
    assert len(bot.messages) == 1


async def test_stale_claim_below_current_threshold_is_cancelled(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        item.priority_score = 50
        reminder = Reminder(
            user_id=user_id,
            item_id=item_id,
            type=PROACTIVE_ATTENTION,
            scheduled_at=now - timedelta(minutes=6),
            created_at=now - timedelta(minutes=6),
            status="CLAIMED",
            payload_json={"attention_score": 99},
        )
        session.add(reminder)
        await session.commit()
        reminder_id = reminder.id

    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 0
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder.status == "CANCELLED"
    assert bot.messages == []


@pytest.mark.parametrize("action", ["done", "archive", "snooze", "disable"])
async def test_claim_is_revalidated_before_telegram_send(session_factory, action):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    prepare = worker._prepare_proactive_send

    async def mutate_before_send(reminder_id, current_user_id, current_item_id, current_now):
        if action == "disable":
            await update_notification_settings(session_factory, 42, attention_enabled=False)
        else:
            await apply_item_action(
                session_factory,
                42,
                item_id,
                action,
                now + timedelta(days=1) if action == "snooze" else None,
            )
        return await prepare(reminder_id, current_user_id, current_item_id, current_now)

    worker._prepare_proactive_send = mutate_before_send
    assert await worker.process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.status == "CANCELLED"
        if action != "disable":
            item = await session.get(Item, item_id)
            expected = {
                "done": ItemState.DONE,
                "archive": ItemState.ARCHIVED,
                "snooze": ItemState.SNOOZED,
            }[action]
            assert item.state is expected


async def test_failed_proactive_send_has_no_budget_gap_or_cooldown_cost(session_factory):
    await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    failing = FakeBot(fail=True)
    assert (
        await ReminderWorker(session_factory, failing, retry_backoff_seconds=0).process_once(now)
        == 0
    )
    async with session_factory() as session:
        failed = await session.scalar(select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION))
        assert failed.status == "FAILED"
        assert failed.sent_at is None
        assert (
            await session.scalar(select(Event.id).where(Event.event_type == "ATTENTION_SHOWN"))
            is None
        )

    succeeding = FakeBot()
    assert (
        await ReminderWorker(session_factory, succeeding).process_once(now + timedelta(seconds=1))
        == 1
    )
    assert len(succeeding.messages) == 1


async def test_two_workers_create_only_one_open_proactive_claim(session_factory):
    await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    first_bot, second_bot = FakeBot(), FakeBot()

    results = await asyncio.gather(
        ReminderWorker(session_factory, first_bot).process_once(now),
        ReminderWorker(session_factory, second_bot).process_once(now),
    )
    assert sum(results) == 1
    assert len(first_bot.messages) + len(second_bot.messages) == 1
    async with session_factory() as session:
        rows = (
            await session.scalars(select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION))
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "SENT"


async def test_proactive_user_failure_does_not_stop_other_users(session_factory):
    first_user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    async with session_factory() as session:
        second_user = User(
            telegram_user_id=1000,
            telegram_chat_id=1000,
            timezone="Europe/Moscow",
            settings_json={"attention_enabled": True, "daily_digest_enabled": False},
        )
        session.add(second_user)
        await session.flush()
        second_user_id = second_user.id
        await session.commit()
    await add_ready_item(session_factory, second_user_id, title="Second user's task")

    worker = ReminderWorker(session_factory, FakeBot())
    process_user = worker._process_proactive_attention

    async def fail_first_user(user_id, now):
        if user_id == first_user_id:
            raise RuntimeError("bad user data")
        return await process_user(user_id, now)

    worker._process_proactive_attention = fail_first_user
    assert await worker.process_once(datetime(2026, 9, 14, 6, 30)) == 1
    assert len(worker.bot.messages) == 1
    assert "Second user's task" in worker.bot.messages[0][1]
