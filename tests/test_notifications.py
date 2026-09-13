import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.actions import apply_item_action
from app.services.notifications import (
    DAILY_DIGEST,
    SNOOZE_RESURFACE,
    ReminderWorker,
    get_notification_settings,
    update_notification_settings,
)
from app.storage.models import Item, Reminder, User


class FakeBot:
    def __init__(self, fail=False, failures=0):
        self.fail = fail
        self.failures = failures
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail or self.failures:
            self.failures = max(0, self.failures - 1)
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))


async def make_ready_item(session_factory, *, state=ItemState.ACTIVE, snoozed_until=None):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42, timezone="Europe/Moscow")
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
    await make_ready_item(session_factory)
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
    await make_ready_item(session_factory)
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
    )
    current = await get_notification_settings(session_factory, 42)
    assert current[1]["daily_digest_time"] == "08:30"
    assert current[1]["quiet_hours_start"] == "20:00"


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
    bot = FakeBot()
    assert await ReminderWorker(session_factory, bot).process_once(datetime(2026, 9, 14, 7, 0)) == 1
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        reminder = await session.scalar(select(Reminder).where(Reminder.item_id == item_id))
        assert item.state is ItemState.ACTIVE
        assert item.snoozed_until is None
        assert reminder.status == "SENT"
    assert "Вернулся" in bot.messages[0][1]


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
