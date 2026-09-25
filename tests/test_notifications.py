import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.domain.enums import ContentKind, ItemState, ItemType, ProcessingStatus, SourceType
from app.domain.models import AttentionHookCandidate, AttentionHookGeneration
from app.llm.base import LlmError
from app.services.actions import apply_item_action
from app.services.attention_hooks import AttentionHookService
from app.services.attention_ranking import AttentionRankingService
from app.services.notifications import (
    ATTENTION_POLICIES,
    DAILY_DIGEST,
    GENERIC_MOTIVATION_DAILY_CAPS,
    MIN_PROACTIVE_ATTENTION_SCORE,
    NOTIFICATION_SEND_TIMEOUT,
    PROACTIVE_ATTENTION,
    PROACTIVE_CLAIM_LEASE,
    SNOOZE_RESURFACE,
    ReminderWorker,
    _local_day_window,
    attention_policy,
    get_notification_settings,
    settings_for,
    update_notification_settings,
)
from app.services.reminder_feedback import ReminderFeedbackService
from app.storage.models import Content, Event, Item, ItemSource, Reminder, User
from tests.fakes import FakeLlmProvider


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


class BarrierBot(FakeBot):
    """Hold the first delivery so tests can inspect a durable in-flight claim."""

    def __init__(self, *, fail_first=False):
        super().__init__()
        self.fail_first = fail_first
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished_first = asyncio.Event()
        self.calls = 0
        self.active_calls = 0
        self.max_active_calls = 0

    async def send_message(self, chat_id, text, **kwargs):
        """Expose an in-flight send window without timing-dependent sleeps."""
        self.calls += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if self.calls == 1:
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.finished_first.set()
                    raise
                if self.fail_first:
                    raise RuntimeError("first Telegram delivery failed")
                self.messages.append((chat_id, text, kwargs))
            else:
                self.messages.append((chat_id, text, kwargs))
        finally:
            self.active_calls -= 1
            if self.calls == 1:
                self.finished_first.set()


async def make_ready_item(
    session_factory,
    *,
    state=ItemState.ACTIVE,
    snoozed_until=None,
    attention_enabled=False,
    telegram_message_id=None,
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
            telegram_message_id=telegram_message_id,
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


async def add_hook_source(session_factory, item_id: int, *, source_index: int, text: str):
    """Persist original evidence and its ItemSource identity for proactive integration tests."""
    async with session_factory() as session:
        source = ItemSource(
            item_id=item_id,
            source_index=source_index,
            source_type=SourceType.WEB,
            source_url=f"https://example.com/source-{source_index}",
            extraction_status="READY",
        )
        session.add(source)
        await session.flush()
        content = Content(
            item_id=item_id,
            source_id=source.id,
            kind=ContentKind.WEB_TEXT,
            text=text,
        )
        session.add(content)
        await session.commit()
        return source.id, content.id


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
    _, item_id = await make_ready_item(session_factory, telegram_message_id=731)
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

    bot = InspectClaimBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    assert len(bot.messages) == 1
    buttons = [
        button for row in bot.messages[0][2]["reply_markup"].inline_keyboard for button in row
    ]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("1", f"item:view:{item_id}")
    ]
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == DAILY_DIGEST))
        assert reminder.status == "SENT"
        assert reminder.sent_at == now


@pytest.mark.parametrize("notification_type", [DAILY_DIGEST, SNOOZE_RESURFACE, PROACTIVE_ATTENTION])
async def test_successful_delivery_timestamp_uses_telegram_completion_time(
    session_factory, monkeypatch, notification_type
):
    """Persist each notification's accepted delivery time, not its cycle-start time."""
    cycle_started_at = datetime(2026, 9, 14, 20, 59, 30)
    completed_at = cycle_started_at + timedelta(minutes=1)
    user_id, item_id = await make_ready_item(
        session_factory, attention_enabled=notification_type == PROACTIVE_ATTENTION
    )
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=notification_type == DAILY_DIGEST,
        daily_digest_time="23:00",
        quiet_hours_start="01:00",
        quiet_hours_end="06:00",
        attention_enabled=notification_type == PROACTIVE_ATTENTION,
        attention_intensity=5,
    )
    if notification_type == DAILY_DIGEST:
        async with session_factory() as session:
            user = await session.get(User, user_id)
            user.created_at = cycle_started_at - timedelta(days=1)
            user.daily_digest_enabled_at = cycle_started_at - timedelta(days=1)
            await session.commit()
    elif notification_type == SNOOZE_RESURFACE:
        await apply_item_action(
            session_factory,
            42,
            item_id,
            "snooze",
            cycle_started_at - timedelta(minutes=1),
        )

    clock = [cycle_started_at]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: clock[0])
    worker = ReminderWorker(session_factory, FakeBot())
    send = worker._send_with_retry

    async def complete_after_telegram_acceptance(*args, **kwargs):
        """Advance the fake clock only after Telegram has accepted the message."""
        await send(*args, **kwargs)
        clock[0] = completed_at

    worker._send_with_retry = complete_after_telegram_acceptance
    assert await worker.process_once() == 1

    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.user_id == user_id, Reminder.type == notification_type)
        )
        assert reminder.status == "SENT"
        assert reminder.sent_at == completed_at
        sent_event = await session.scalar(
            select(Event).where(
                Event.reminder_id == reminder.id,
                Event.event_type == "REMINDER_SENT",
            )
        )
        assert sent_event is not None
        assert sent_event.item_id == reminder.item_id
        assert sent_event.created_at == completed_at
        assert sent_event.payload_json["reminder_type"] == notification_type


async def test_delivery_crossing_local_midnight_counts_on_completion_day(
    session_factory, monkeypatch
):
    """A successful proactive send consumes PM-08 budget on Telegram's acceptance date."""
    cycle_started_at = datetime(2026, 9, 14, 20, 59, 30)
    completed_at = cycle_started_at + timedelta(minutes=1)
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        attention_intensity=1,
        quiet_hours_start="01:00",
        quiet_hours_end="06:00",
    )
    clock = [cycle_started_at]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: clock[0])
    worker = ReminderWorker(session_factory, FakeBot())
    send = worker._send_with_retry

    async def complete_after_telegram_acceptance(*args, **kwargs):
        """Advance the fake clock only after Telegram has accepted the message."""
        await send(*args, **kwargs)
        clock[0] = completed_at

    worker._send_with_retry = complete_after_telegram_acceptance
    assert await worker.process_once() == 1

    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.sent_at == completed_at
        user = await session.get(User, user_id)
        _, _, local_date, blocked = await worker._attention_gate(
            session, user, completed_at + timedelta(hours=9)
        )
        assert local_date.isoformat() == "2026-09-15"
        assert blocked == "daily_cap"


async def test_minimum_gap_is_anchored_to_delivery_completion(session_factory, monkeypatch):
    """The ninety-minute PM-08 gap starts when Telegram accepts the send."""
    cycle_started_at = datetime(2026, 9, 14, 12, 0)
    completed_at = cycle_started_at + timedelta(minutes=1)
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        attention_intensity=5,
    )
    clock = [cycle_started_at]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: clock[0])
    worker = ReminderWorker(session_factory, FakeBot())
    send = worker._send_with_retry

    async def complete_after_telegram_acceptance(*args, **kwargs):
        """Advance the fake clock only after Telegram has accepted the message."""
        await send(*args, **kwargs)
        clock[0] = completed_at

    worker._send_with_retry = complete_after_telegram_acceptance
    assert await worker.process_once() == 1

    async with session_factory() as session:
        user = await session.get(User, user_id)
        _, _, _, blocked = await worker._attention_gate(
            session,
            user,
            completed_at + timedelta(minutes=90) - timedelta(seconds=1),
        )
        assert blocked == "minimum_gap"


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
        update_notification_settings(session_factory, 42, generic_motivation_enabled=False),
    )
    current = await get_notification_settings(session_factory, 42)
    assert current[1]["daily_digest_time"] == "08:30"
    assert current[1]["quiet_hours_start"] == "20:00"
    assert current[1]["attention_enabled"] is True
    assert current[1]["attention_intensity"] == 4
    assert current[1]["generic_motivation_enabled"] is False


async def test_transient_notification_failure_retries_before_terminal_failure(session_factory):
    await make_ready_item(session_factory)
    bot = FakeBot(failures=2)
    worker = ReminderWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_once(datetime(2026, 9, 14, 6, 30)) == 1
    assert len(bot.messages) == 1


async def test_snoozed_item_becomes_active_and_notifies(session_factory):
    until = datetime(2026, 9, 14, 6, 0)
    _, item_id = await make_ready_item(
        session_factory,
        state=ItemState.ACTIVE,
        snoozed_until=None,
        telegram_message_id=732,
    )
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
    callbacks = {
        button.callback_data
        for row in bot.messages[0][2]["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data
    }
    assert f"item:original:{item_id}" in callbacks


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
    assert current[1]["generic_motivation_enabled"] is True

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
    assert settings_for(User(settings_json={}))["generic_motivation_enabled"] is True


async def test_generic_motivation_setting_is_strict_and_independent(session_factory):
    await make_ready_item(session_factory)
    current = await get_notification_settings(session_factory, 42)
    assert current[1]["generic_motivation_enabled"] is True

    updated = await update_notification_settings(
        session_factory, 42, generic_motivation_enabled=False
    )
    assert updated[1]["generic_motivation_enabled"] is False
    assert updated[1]["attention_enabled"] is False
    for value in (1, 0, "true"):
        with pytest.raises(ValueError):
            await update_notification_settings(
                session_factory, 42, generic_motivation_enabled=value
            )
    unchanged = await update_notification_settings(
        session_factory, 42, generic_motivation_enabled=None
    )
    assert unchanged[1]["generic_motivation_enabled"] is False
    assert dict(GENERIC_MOTIVATION_DAILY_CAPS) == {1: 0, 2: 1, 3: 1, 4: 1, 5: 2}


async def test_proactive_delivery_persists_reminder_and_exposure(session_factory):
    user_id, item_id = await make_ready_item(
        session_factory, attention_enabled=True, telegram_message_id=733
    )
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
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data is not None
    }
    urls = {
        button.url for row in kwargs["reply_markup"].inline_keyboard for button in row if button.url
    }
    assert all(
        button.callback_data is None
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.url
    )
    assert any(value.startswith("reminder:done:") for value in callbacks)
    assert any(value.startswith("reminder:later:") for value in callbacks)
    assert any(value.startswith("reminder:dismiss:") for value in callbacks)
    assert any(value.startswith("reminder:less:") for value in callbacks)
    assert any(value.startswith("reminder:open:") for value in callbacks)
    assert any(value.startswith("reminder:original:") for value in callbacks)
    assert not any(value.startswith("item:done:") for value in callbacks)
    assert not any(value.startswith("item:archive:") for value in callbacks)
    assert "https://www.youtube.com/watch?v=example" in urls

    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        event = await session.scalar(select(Event).where(Event.event_type == "ATTENTION_SHOWN"))
        sent_event = await session.scalar(
            select(Event).where(
                Event.reminder_id == reminder.id,
                Event.event_type == "REMINDER_SENT",
            )
        )
        assert reminder.user_id == user_id
        assert reminder.item_id == item_id
        assert (
            await session.scalar(
                select(Event.id).where(
                    Event.reminder_id == reminder.id,
                    Event.event_type == "REMINDER_OPENED",
                )
            )
            is None
        )
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
        assert sent_event.item_id == item_id
        assert sent_event.payload_json.get("category") == reminder.payload_json.get("category")
        assert sent_event.payload_json["item_type"] == reminder.payload_json["item_type"]
        assert sent_event.created_at == now
        assert sent_event.idempotency_key == f"reminder:{reminder.id}:sent"
        ranked = await AttentionRankingService().list_candidates(session, user_id, now=now)
        assert ranked[0][0].id == item_id
        assert ranked[0][1].recent_show_penalty == -25


async def test_proactive_hook_keeps_source_provenance_and_focuses_its_action(session_factory):
    """Enrich only the claimed proactive card and put its supporting source first."""
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    await add_hook_source(
        session_factory,
        item_id,
        source_index=0,
        text="An unrelated article about interface layout.",
    )
    supporting_source_id, evidence_id = await add_hook_source(
        session_factory,
        item_id,
        source_index=1,
        text="The method reduced cache misses by 40%.",
    )
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[
                AttentionHookCandidate(
                    hook_type="PRACTICAL_VALUE",
                    text="It reduced repeated work.",
                    evidence_excerpt="The method reduced cache misses by 40%",
                    source_content_id=evidence_id,
                )
            ]
        )
    )
    service = AttentionHookService(session_factory, provider)
    bot = FakeBot()
    now = datetime(2026, 9, 14, 6, 30)

    assert (
        await ReminderWorker(
            session_factory,
            bot,
            attention_hook_service=service,
        ).process_once(now)
        == 1
    )

    _, message, kwargs = bot.messages[0]
    assert "Одна сильная мысль внутри:" in message
    assert "It reduced repeated work." in message
    assert "Почему сейчас:" in message
    urls = [
        button.url for row in kwargs["reply_markup"].inline_keyboard for button in row if button.url
    ]
    assert urls[0] == "https://example.com/source-1"
    async with session_factory() as session:
        hook = await session.scalar(
            select(Content).where(
                Content.item_id == item_id,
                Content.kind == ContentKind.ATTENTION_HOOK,
            )
        )
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert hook.source_id == supporting_source_id
        assert reminder.status == "SENT"
        assert reminder.payload_json["hook_content_id"] == hook.id
        assert reminder.payload_json["template_id"] == "strong_thought_v1"
        assert reminder.payload_json["attention_score"] >= MIN_PROACTIVE_ATTENTION_SCORE
        assert reminder.payload_json["priority_score"] == 80
        assert "evidence_excerpt" not in reminder.payload_json
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(Event.event_type == "ATTENTION_SHOWN")
            )
            == 1
        )


async def test_hook_provider_failure_keeps_the_normal_proactive_reminder(session_factory):
    """Treat hook generation failure as an optional enhancement and send the PM-07 fallback."""
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    await add_hook_source(
        session_factory,
        item_id,
        source_index=0,
        text="An article with enough saved content for a hook request.",
    )
    provider = FakeLlmProvider(attention_hook_error=LlmError("LLM_FAILED", "provider unavailable"))
    bot = FakeBot()
    result = await ReminderWorker(
        session_factory,
        bot,
        attention_hook_service=AttentionHookService(session_factory, provider),
    ).process_once(datetime(2026, 9, 14, 6, 30))

    assert result == 1
    assert len(provider.attention_hook_calls) == 1
    assert "Почему сейчас:" in bot.messages[0][1]
    assert "Одна сильная мысль внутри:" not in bot.messages[0][1]
    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.status == "SENT"
        assert "hook_content_id" not in reminder.payload_json
        assert "template_id" not in reminder.payload_json
        assert (
            await session.scalar(
                select(func.count(Content.id)).where(Content.kind == ContentKind.ATTENTION_HOOK)
            )
            == 0
        )


async def test_hook_timeout_still_sends_fallback_without_resetting_claim(
    session_factory, monkeypatch
):
    """Bound the hook call within the claim window and continue to Telegram after timeout."""
    _user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    await add_hook_source(
        session_factory,
        item_id,
        source_index=0,
        text="An article with enough saved content for a hook request.",
    )
    monkeypatch.setattr("app.services.notifications.ATTENTION_HOOK_TIMEOUT_SECONDS", 0.01)
    provider = FakeLlmProvider()
    cancelled = asyncio.Event()

    async def never_finishes(source_context: str, *, preferred_language: str):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.generate_attention_hooks = never_finishes
    service = AttentionHookService(session_factory, provider)
    original_for_reminder = service.for_reminder
    observed_claims = []

    async def track_claim(**kwargs):
        async with session_factory() as session:
            before = (await session.get(Reminder, kwargs["reminder_id"])).claimed_at
        result = await original_for_reminder(**kwargs)
        async with session_factory() as session:
            after = (await session.get(Reminder, kwargs["reminder_id"])).claimed_at
        observed_claims.append((before, after))
        return result

    service.for_reminder = track_claim
    bot = FakeBot()
    result = await ReminderWorker(
        session_factory, bot, attention_hook_service=service
    ).process_once(datetime(2026, 9, 14, 6, 30))

    assert result == 1
    assert cancelled.is_set()
    assert observed_claims and observed_claims[0][0] == observed_claims[0][1]
    assert "Почему сейчас:" in bot.messages[0][1]
    assert "Одна сильная мысль внутри:" not in bot.messages[0][1]


async def test_item_done_during_hook_generation_fails_final_pm08_revalidation(session_factory):
    """A hook result cannot bypass the normal final lifecycle check before sending."""
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    await add_hook_source(
        session_factory,
        item_id,
        source_index=0,
        text="The method reduced cache misses by 40%.",
    )
    async with session_factory() as session:
        evidence = await session.scalar(
            select(Content).where(Content.item_id == item_id, Content.kind == ContentKind.WEB_TEXT)
        )
        evidence_id = evidence.id
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[
                AttentionHookCandidate(
                    hook_type="PRACTICAL_VALUE",
                    text="It reduces repeated work.",
                    evidence_excerpt="The method reduced cache misses by 40%",
                    source_content_id=evidence_id,
                )
            ]
        )
    )
    generate = provider.generate_attention_hooks

    async def mark_done_after_generation(source_context: str, *, preferred_language: str):
        result = await generate(source_context, preferred_language=preferred_language)
        await apply_item_action(session_factory, 42, item_id, "done")
        return result

    provider.generate_attention_hooks = mark_done_after_generation
    bot = FakeBot()

    assert (
        await ReminderWorker(
            session_factory,
            bot,
            attention_hook_service=AttentionHookService(session_factory, provider),
        ).process_once(datetime(2026, 9, 14, 6, 30))
        == 0
    )
    assert bot.messages == []
    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        item = await session.get(Item, item_id)
        assert reminder.status == "CANCELLED"
        assert item.state is ItemState.DONE
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(Event.event_type == "ATTENTION_SHOWN")
            )
            == 0
        )


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


async def test_dismissal_cooldown_boundary_and_pm08_longer_delay(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    now = datetime(2026, 9, 24, 12)
    async with session_factory() as session:
        old_reminder = Reminder(
            user_id=user_id,
            item_id=item_id,
            type=PROACTIVE_ATTENTION,
            scheduled_at=now - timedelta(days=3),
            status="SENT",
            sent_at=now - timedelta(days=3),
            payload_json={"reminder_type": PROACTIVE_ATTENTION},
        )
        session.add(old_reminder)
        await session.flush()
        recent_dismissal = Event(
            user_id=user_id,
            item_id=item_id,
            reminder_id=old_reminder.id,
            event_type="REMINDER_DISMISSED",
            payload_json={"reminder_type": PROACTIVE_ATTENTION},
            created_at=now - timedelta(hours=23, minutes=59),
        )
        session.add(recent_dismissal)
        await session.commit()
        latest = await ReminderFeedbackService.latest_dismissals(
            session, user_id, [item_id], now=now
        )
        assert ReminderWorker._proactive_cooldown_active(
            item_id, now, attention_policy(5), {}, latest
        )
        assert not ReminderWorker._proactive_cooldown_active(
            item_id,
            now,
            attention_policy(5),
            {},
            {item_id: now - timedelta(hours=24)},
        )
        assert ReminderWorker._proactive_cooldown_active(
            item_id,
            now,
            attention_policy(1),
            {item_id: now - timedelta(hours=24)},
            {item_id: now - timedelta(hours=25)},
        )


async def test_dismissed_top_candidate_falls_through_to_next(session_factory):
    user_id, dismissed_item_id = await make_ready_item(session_factory, attention_enabled=True)
    next_item_id = await add_ready_item(session_factory, user_id, title="Next task", priority=85)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        attention_intensity=5,
        generic_motivation_enabled=False,
    )
    now = datetime(2026, 9, 24, 12)
    async with session_factory() as session:
        for item_id, priority, interest in (
            (dismissed_item_id, 100, 3),
            (next_item_id, 85, 2),
        ):
            item = await session.get(Item, item_id)
            item.priority_score = priority
            item.interest_level = interest
            item.category = "AI"
            item.created_at = now - timedelta(days=120)
        prior = Reminder(
            user_id=user_id,
            item_id=dismissed_item_id,
            type=PROACTIVE_ATTENTION,
            scheduled_at=now - timedelta(days=3),
            status="SENT",
            sent_at=now - timedelta(days=3),
            payload_json={"reminder_type": PROACTIVE_ATTENTION},
        )
        session.add(prior)
        await session.flush()
        session.add(
            Event(
                user_id=user_id,
                item_id=dismissed_item_id,
                reminder_id=prior.id,
                event_type="REMINDER_DISMISSED",
                payload_json={
                    "reminder_type": PROACTIVE_ATTENTION,
                    "category": "AI",
                    "item_type": "ACTION",
                },
                created_at=now - timedelta(hours=23, minutes=59),
            )
        )
        await session.commit()
        ranked = await AttentionRankingService().list_candidates(session, user_id, now=now)
        assert ranked[0][0].id == dismissed_item_id

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
        assert sent.item_id == next_item_id


async def test_dismissal_is_rechecked_inside_final_proactive_prepare(session_factory):
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        attention_intensity=5,
        generic_motivation_enabled=False,
    )
    now = datetime(2026, 9, 24, 12)
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        item.priority_score = 100
        item.interest_level = 3
        item.category = "AI"
        item.created_at = now - timedelta(days=120)
        prior = Reminder(
            user_id=user_id,
            item_id=item_id,
            type=PROACTIVE_ATTENTION,
            scheduled_at=now - timedelta(days=3),
            status="SENT",
            sent_at=now - timedelta(days=3),
            payload_json={"reminder_type": PROACTIVE_ATTENTION},
        )
        session.add(prior)
        await session.commit()
        prior_id = prior.id

    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    prepare = worker._prepare_proactive_send

    async def dismiss_after_claim(reminder_id, current_user_id, current_item_id, generation, clock):
        async with session_factory() as session:
            session.add(
                Event(
                    user_id=user_id,
                    item_id=item_id,
                    reminder_id=prior_id,
                    event_type="REMINDER_DISMISSED",
                    payload_json={"reminder_type": PROACTIVE_ATTENTION},
                    created_at=clock,
                )
            )
            await session.commit()
        return await prepare(reminder_id, current_user_id, current_item_id, generation, clock)

    worker._prepare_proactive_send = dismiss_after_claim
    assert await worker.process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        claimed = await session.scalar(
            select(Reminder)
            .where(
                Reminder.type == PROACTIVE_ATTENTION,
                Reminder.id != prior_id,
            )
            .order_by(Reminder.id.desc())
        )
        assert claimed.status == "CANCELLED"
        assert (
            await session.scalar(
                select(Event.id).where(
                    Event.reminder_id == claimed.id,
                    Event.event_type == "REMINDER_SENT",
                )
            )
            is None
        )


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

    async def mutate_before_send(
        reminder_id, current_user_id, current_item_id, claim_generation, current_now
    ):
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
        return await prepare(
            reminder_id,
            current_user_id,
            current_item_id,
            claim_generation,
            current_now,
        )

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


async def test_current_pm07_score_is_revalidated_before_telegram_send(session_factory):
    """A new exposure penalty can invalidate a previously qualified score."""
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    prepare = worker._prepare_proactive_send

    async def record_manual_exposure_before_send(
        reminder_id, current_user_id, current_item_id, claim_generation, current_now
    ):
        async with session_factory() as session:
            session.add(
                Event(
                    user_id=user_id,
                    item_id=item_id,
                    event_type="ATTENTION_SHOWN",
                    created_at=now,
                )
            )
            await session.commit()
        return await prepare(
            reminder_id,
            current_user_id,
            current_item_id,
            claim_generation,
            current_now,
        )

    worker._prepare_proactive_send = record_manual_exposure_before_send
    assert await worker.process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        current_ranked = await AttentionRankingService().list_candidates(session, user_id, now=now)
        assert current_ranked[0][1].score < MIN_PROACTIVE_ATTENTION_SCORE
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.status == "CANCELLED"


async def test_prepare_uses_current_time_after_worker_crosses_quiet_hours(
    session_factory, monkeypatch
):
    """A candidate claimed before quiet hours is stopped if preparation crosses the boundary."""
    user_id, _ = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=False,
        attention_enabled=True,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
    )
    clock = [datetime(2026, 9, 14, 18, 59, 20)]  # 21:59:20 in Europe/Moscow
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: clock[0])
    bot = FakeBot()
    worker = ReminderWorker(session_factory, bot)
    prepare = worker._prepare_proactive_send

    async def cross_quiet_boundary_before_prepare(
        reminder_id, current_user_id, item_id, claim_generation, current_now
    ):
        """Simulate a worker pause while the claim remains inside its send deadline."""
        clock[0] += timedelta(minutes=1)
        return await prepare(reminder_id, current_user_id, item_id, claim_generation, current_now)

    worker._prepare_proactive_send = cross_quiet_boundary_before_prepare
    assert await worker.process_once() == 0
    assert bot.messages == []
    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(
                Reminder.user_id == user_id, Reminder.type == PROACTIVE_ATTENTION
            )
        )
        assert reminder.status == "CLAIMED"


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
        assert (
            await session.scalar(select(Event.id).where(Event.event_type == "REMINDER_SENT"))
            is None
        )

    succeeding = FakeBot()
    assert (
        await ReminderWorker(session_factory, succeeding).process_once(now + timedelta(seconds=1))
        == 1
    )
    assert len(succeeding.messages) == 1


@pytest.mark.parametrize("notification_type", [DAILY_DIGEST, SNOOZE_RESURFACE])
@pytest.mark.parametrize("first_send_fails", [False, True])
async def test_in_flight_digest_or_snooze_reserves_user_from_proactive_send(
    session_factory, notification_type, first_send_fails
):
    """Prove another worker waits for cross-type delivery to resolve before pacing."""
    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    now = datetime(2026, 9, 14, 6, 30)
    await update_notification_settings(
        session_factory,
        42,
        daily_digest_enabled=notification_type == DAILY_DIGEST,
        attention_enabled=True,
        attention_intensity=1,
    )
    if notification_type == SNOOZE_RESURFACE:
        await apply_item_action(session_factory, 42, item_id, "snooze", now - timedelta(minutes=1))

    first_bot = BarrierBot(fail_first=first_send_fails)
    first_worker = ReminderWorker(
        session_factory, first_bot, max_send_attempts=1, retry_backoff_seconds=0
    )
    first_cycle = asyncio.create_task(first_worker.process_once(now))
    await asyncio.wait_for(first_bot.started.wait(), timeout=1)

    second_bot = FakeBot()
    second_cycle = await ReminderWorker(session_factory, second_bot).process_once(
        now + timedelta(seconds=1)
    )
    assert second_cycle == 0
    assert second_bot.messages == []

    first_bot.release.set()
    assert await asyncio.wait_for(first_cycle, timeout=1) == 1
    assert first_bot.max_active_calls == 1
    assert len(first_bot.messages) == 1
    async with session_factory() as session:
        first_reminder = await session.scalar(
            select(Reminder).where(Reminder.type == notification_type)
        )
        assert first_reminder.status == ("FAILED" if first_send_fails else "SENT")
        proactive = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        if first_send_fails:
            assert proactive is not None and proactive.status == "SENT"
        else:
            assert proactive is None


async def test_send_timeout_precedes_lease_recovery_and_generation_fences_old_owner(
    session_factory, monkeypatch
):
    """Verify timeout ends a sender before recovery and stale writes are fenced."""
    assert NOTIFICATION_SEND_TIMEOUT < PROACTIVE_CLAIM_LEASE
    lease = timedelta(milliseconds=120)
    timeout = timedelta(milliseconds=60)
    monkeypatch.setattr("app.services.notifications.PROACTIVE_CLAIM_LEASE", lease)
    monkeypatch.setattr("app.services.notifications.NOTIFICATION_SEND_TIMEOUT", timeout)
    assert timeout < lease

    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    lease_clock = [now]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: lease_clock[0])
    first_bot = BarrierBot()
    first_worker = ReminderWorker(session_factory, first_bot, retry_backoff_seconds=0)

    # Model process loss after the bounded Telegram call has timed out but
    # before the old owner can persist its terminal state.
    async def leave_claim_for_recovery(reminder_id, claim_generation, status):
        return None

    first_worker._set_proactive_status = leave_claim_for_recovery
    first_cycle = asyncio.create_task(first_worker.process_once(now))
    await asyncio.wait_for(first_bot.started.wait(), timeout=1)
    assert await asyncio.wait_for(first_bot.finished_first.wait(), timeout=1)
    assert await asyncio.wait_for(first_cycle, timeout=1) == 0
    assert first_bot.active_calls == 0

    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.status == "CLAIMED"
        old_generation = reminder.claim_generation
        reminder_id = reminder.id

    recovered_bot = FakeBot()
    recovered_at = now + lease + timedelta(milliseconds=1)
    lease_clock[0] = recovered_at
    assert await ReminderWorker(session_factory, recovered_bot).process_once(recovered_at) == 1
    assert len(recovered_bot.messages) == 1

    # A delayed completion from the expired owner must not finalize or fail
    # the claim now owned by the recovery worker.
    await first_worker._set_proactive_status(reminder_id, old_generation, "FAILED")
    await first_worker._finalize_proactive_send(
        reminder_id, user_id, item_id, old_generation, recovered_at
    )
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder.status == "SENT"
        assert reminder.claim_generation == old_generation + 1
        assert reminder.sent_at == recovered_at
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(Event.event_type == "ATTENTION_SHOWN")
            )
            == 1
        )


async def test_recovered_claim_owner_does_not_start_telegram_after_claim_deadline(
    session_factory, monkeypatch
):
    """An owner delayed after prepare must not send after recovery takes its generation."""
    lease = timedelta(milliseconds=120)
    timeout = timedelta(milliseconds=60)
    monkeypatch.setattr("app.services.notifications.PROACTIVE_CLAIM_LEASE", lease)
    monkeypatch.setattr("app.services.notifications.NOTIFICATION_SEND_TIMEOUT", timeout)

    user_id, item_id = await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    lease_clock = [now]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: lease_clock[0])

    first_bot = BarrierBot()
    first_worker = ReminderWorker(session_factory, first_bot, retry_backoff_seconds=0)
    prepare = first_worker._prepare_proactive_send
    prepared = asyncio.Event()
    resume_old_owner = asyncio.Event()

    async def pause_after_prepare(
        reminder_id, current_user_id, current_item_id, claim_generation, current_now
    ):
        result = await prepare(
            reminder_id,
            current_user_id,
            current_item_id,
            claim_generation,
            current_now,
        )
        prepared.set()
        await resume_old_owner.wait()
        return result

    first_worker._prepare_proactive_send = pause_after_prepare
    first_cycle = asyncio.create_task(first_worker.process_once())
    await asyncio.wait_for(prepared.wait(), timeout=1)

    recovered_at = now + lease + timedelta(milliseconds=1)
    lease_clock[0] = recovered_at
    recovered_bot = FakeBot()
    assert await ReminderWorker(session_factory, recovered_bot).process_once() == 1
    assert len(recovered_bot.messages) == 1

    resume_old_owner.set()
    assert await asyncio.wait_for(first_cycle, timeout=1) == 0
    assert first_bot.calls == 0

    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.status == "SENT"
        assert reminder.claim_generation == 2
        assert reminder.sent_at == recovered_at
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(Event.event_type == "ATTENTION_SHOWN")
            )
            == 1
        )


async def test_expired_claim_is_rejected_before_preparation_and_then_recovered(
    session_factory, monkeypatch
):
    """A paused owner cannot prepare an expired claim; the next owner can recover it."""
    lease = timedelta(milliseconds=120)
    timeout = timedelta(milliseconds=60)
    monkeypatch.setattr("app.services.notifications.PROACTIVE_CLAIM_LEASE", lease)
    monkeypatch.setattr("app.services.notifications.NOTIFICATION_SEND_TIMEOUT", timeout)

    await make_ready_item(session_factory, attention_enabled=True)
    await update_notification_settings(
        session_factory, 42, daily_digest_enabled=False, attention_enabled=True
    )
    now = datetime(2026, 9, 14, 6, 30)
    lease_clock = [now]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: lease_clock[0])

    first_bot = FakeBot()
    first_worker = ReminderWorker(session_factory, first_bot)
    prepare = first_worker._prepare_proactive_send
    claimed = asyncio.Event()
    resume_old_owner = asyncio.Event()

    async def pause_before_prepare(
        reminder_id, current_user_id, current_item_id, claim_generation, current_now
    ):
        claimed.set()
        await resume_old_owner.wait()
        return await prepare(
            reminder_id,
            current_user_id,
            current_item_id,
            claim_generation,
            current_now,
        )

    first_worker._prepare_proactive_send = pause_before_prepare
    first_cycle = asyncio.create_task(first_worker.process_once())
    await asyncio.wait_for(claimed.wait(), timeout=1)

    recovered_at = now + lease + timedelta(milliseconds=1)
    lease_clock[0] = recovered_at
    resume_old_owner.set()
    assert await asyncio.wait_for(first_cycle, timeout=1) == 0
    assert first_bot.messages == []

    recovered_bot = FakeBot()
    assert await ReminderWorker(session_factory, recovered_bot).process_once() == 1
    assert len(recovered_bot.messages) == 1
    async with session_factory() as session:
        reminder = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert reminder.status == "SENT"
        assert reminder.claim_generation == 2


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
