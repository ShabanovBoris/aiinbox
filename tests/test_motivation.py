import asyncio
from datetime import UTC, date, datetime, time, timedelta
from string import Formatter
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.domain.enums import ItemState, ItemType, MotivationKind, ProcessingStatus, SourceType
from app.services.calendar_windows import local_day_window
from app.services.motivation import _TEMPLATES, MotivationService, _next_template
from app.services.notifications import MOTIVATION_NUDGE, PROACTIVE_ATTENTION, ReminderWorker
from app.storage.models import Event, Item, Reminder, User


async def add_user(session_factory, *, telegram_id=42, timezone="UTC", settings=None):
    """Build one canonical user row for isolated domain-fact queries."""
    async with session_factory() as session:
        user = User(
            telegram_user_id=telegram_id,
            telegram_chat_id=telegram_id,
            timezone=timezone,
            settings_json=settings or {},
        )
        session.add(user)
        await session.commit()
        return user.id


async def add_item(
    session_factory,
    user_id,
    *,
    created_at,
    item_type=ItemType.ACTION,
    state=ItemState.ACTIVE,
    processing=ProcessingStatus.READY,
    priority=40,
    interest=2,
    minutes=None,
    title="Task",
):
    """Persist only canonical Item metadata consumed by motivation aggregates."""
    async with session_factory() as session:
        item = Item(
            user_id=user_id,
            processing_status=processing,
            state=state,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note=title,
            item_type=item_type,
            title=title,
            priority_score=priority,
            interest_level=interest,
            estimated_action_minutes=minutes,
            created_at=created_at,
        )
        session.add(item)
        await session.commit()
        return item.id


async def add_event(session_factory, user_id, item_id, event_type, created_at):
    """Append durable lifecycle history used by calendar-derived facts."""
    async with session_factory() as session:
        session.add(
            Event(
                user_id=user_id,
                item_id=item_id,
                event_type=event_type,
                created_at=created_at,
            )
        )
        await session.commit()


async def candidates(session_factory, user_id, now, timezone="UTC"):
    """Run the production domain service against the fixture's async session."""
    async with session_factory() as session:
        return await MotivationService().candidates(
            session, user_id, zone=ZoneInfo(timezone), now=now
        )


class RecordingBot:
    """Observe Telegram-facing output without crossing the delivery adapter boundary."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("Telegram unavailable")
        self.messages.append((chat_id, text, kwargs))


class BlockingBot(RecordingBot):
    """Expose a deterministic in-flight send window for cross-worker claim tests."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, chat_id, text, **kwargs):
        self.started.set()
        await self.release.wait()
        self.messages.append((chat_id, text, kwargs))


class HookSpy:
    """Prove the generic path never crosses the PM-09 Item-hook boundary."""

    def __init__(self):
        self.calls = 0

    async def for_reminder(self, **kwargs):
        self.calls += 1
        raise AssertionError("generic motivation must not request an Item hook")


async def make_worker_user(
    session_factory,
    *,
    level=3,
    attention=True,
    motivation=True,
    timezone="UTC",
    telegram_id=42,
):
    """Create settings that isolate PM-10 from the daily digest in worker tests."""
    return await add_user(
        session_factory,
        telegram_id=telegram_id,
        timezone=timezone,
        settings={
            "daily_digest_enabled": False,
            "attention_enabled": attention,
            "attention_intensity": level,
            "generic_motivation_enabled": motivation,
            "quiet_hours_start": "22:30",
            "quiet_hours_end": "08:00",
        },
    )


async def add_growth_items(session_factory, user_id, now, count=3):
    """Create non-actionable same-day Items so only the generic fact is eligible."""
    return [
        await add_item(
            session_factory,
            user_id,
            created_at=now,
            item_type=ItemType.REFERENCE,
            processing=ProcessingStatus.QUEUED,
            title=f"reference {index}",
        )
        for index in range(count)
    ]


async def add_proactive_item(session_factory, user_id, now, *, title):
    """Persist a strong actionable Item for proactive-versus-generic arbitration."""
    return await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=30),
        priority=100,
        interest=3,
        item_type=ItemType.ACTION,
        title=title,
    )


async def test_item_facts_use_exact_thresholds_actionability_and_user_scope(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await add_user(session_factory)
    other_user_id = await add_user(session_factory, telegram_id=1000)

    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=30),
        priority=75,
        title="exactly thirty days",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=30) + timedelta(seconds=1),
        priority=100,
        title="one second too new",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=45),
        priority=74,
        title="priority below stale threshold",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=14),
        priority=10,
        interest=3,
        title="exactly two weeks",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=14) + timedelta(seconds=1),
        interest=3,
        title="one second too new for interest",
    )
    for index in range(3):
        await add_item(
            session_factory,
            user_id,
            created_at=now - timedelta(days=2),
            priority=60,
            minutes=20,
            title=f"quick win {index}",
        )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=2),
        priority=60,
        minutes=21,
        title="too long",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=2),
        priority=59,
        minutes=20,
        title="priority too low",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=45),
        priority=100,
        minutes=10,
        item_type=ItemType.REFERENCE,
        title="not actionable",
    )
    await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=45),
        priority=100,
        minutes=10,
        state=ItemState.DONE,
        title="resolved",
    )
    for index in range(3):
        await add_item(
            session_factory,
            other_user_id,
            created_at=now - timedelta(days=2),
            priority=60,
            minutes=20,
            title=f"another user's quick win {index}",
        )

    result = await candidates(session_factory, user_id, now)
    by_kind = {candidate.kind: candidate for candidate in result}
    assert [candidate.kind for candidate in result] == [
        MotivationKind.STALE_IMPORTANT,
        MotivationKind.HIGH_INTEREST_STALE,
        MotivationKind.QUICK_WINS,
    ]
    assert dict(by_kind[MotivationKind.STALE_IMPORTANT].facts) == {"count": 1}
    assert dict(by_kind[MotivationKind.HIGH_INTEREST_STALE].facts) == {"count": 1}
    assert dict(by_kind[MotivationKind.QUICK_WINS].facts) == {
        "count": 3,
        "max_minutes": 20,
    }
    assert "3" in by_kind[MotivationKind.QUICK_WINS].rendered_text
    assert "20" in by_kind[MotivationKind.QUICK_WINS].rendered_text
    with pytest.raises(TypeError):
        by_kind[MotivationKind.QUICK_WINS].facts["count"] = 99

    other_result = await candidates(session_factory, other_user_id, now)
    assert len(other_result) == 1
    assert dict(other_result[0].facts) == {"count": 3, "max_minutes": 20}


async def test_inbox_growth_uses_half_open_local_day_and_only_resolution_events(session_factory):
    zone = ZoneInfo("Europe/Helsinki")
    now = datetime(2026, 3, 29, 10)  # 13:00 after the local DST transition
    user_id = await add_user(session_factory, timezone="Europe/Helsinki")
    local_date, start, end = local_day_window(now, zone)
    assert local_date == date(2026, 3, 29)
    assert end - start == timedelta(hours=23)

    item_ids = []
    for index, created_at in enumerate(
        [
            start,
            start + timedelta(hours=1),
            start + timedelta(hours=2),
            start + timedelta(hours=3),
            end - timedelta(seconds=1),
        ]
    ):
        item_ids.append(
            await add_item(
                session_factory,
                user_id,
                created_at=created_at,
                item_type=ItemType.REFERENCE,
                processing=ProcessingStatus.QUEUED,
                title=f"reference {index}",
            )
        )
    await add_item(
        session_factory,
        user_id,
        created_at=end,
        item_type=ItemType.REFERENCE,
        processing=ProcessingStatus.QUEUED,
        title="exactly next local day",
    )
    await add_event(session_factory, user_id, item_ids[0], "DONE", start)
    await add_event(session_factory, user_id, item_ids[1], "ARCHIVED", end - timedelta(seconds=1))
    await add_event(session_factory, user_id, item_ids[2], "SNOOZED", end - timedelta(seconds=1))
    await add_event(session_factory, user_id, item_ids[3], "ARCHIVED", end)

    result = await candidates(session_factory, user_id, now, timezone="Europe/Helsinki")
    assert [candidate.kind for candidate in result] == [MotivationKind.INBOX_GROWTH]
    assert dict(result[0].facts) == {"created": 5, "resolved": 2, "net": 3}


async def test_completion_streak_deduplicates_local_dates_and_week_has_seven_dates(
    session_factory,
):
    zone = ZoneInfo("Europe/Helsinki")
    now = datetime(2026, 3, 30, 12, tzinfo=UTC)
    today = now.astimezone(zone).date()
    user_id = await add_user(session_factory, timezone="Europe/Helsinki")
    item_id = await add_item(
        session_factory,
        user_id,
        created_at=datetime(2026, 1, 1),
        item_type=ItemType.REFERENCE,
        processing=ProcessingStatus.QUEUED,
    )

    for local_day in (today - timedelta(days=2), today - timedelta(days=1), today):
        instant = datetime.combine(local_day, time(12), tzinfo=zone).astimezone(UTC)
        await add_event(session_factory, user_id, item_id, "DONE", instant.replace(tzinfo=None))
    today_noon = datetime.combine(today, time(13), tzinfo=zone).astimezone(UTC)
    await add_event(
        session_factory, user_id, item_id, "DONE", today_noon.replace(tzinfo=None)
    )  # multiple completions on one date remain one streak day
    week_start = today - timedelta(days=6)
    week_start_utc = datetime.combine(week_start, time.min, tzinfo=zone).astimezone(UTC)
    await add_event(
        session_factory,
        user_id,
        item_id,
        "DONE",
        week_start_utc.replace(tzinfo=None),
    )
    outside_week = datetime.combine(today - timedelta(days=7), time.min, tzinfo=zone).astimezone(
        UTC
    )
    await add_event(
        session_factory,
        user_id,
        item_id,
        "DONE",
        outside_week.replace(tzinfo=None),
    )

    result = await candidates(session_factory, user_id, now, timezone="Europe/Helsinki")
    by_kind = {candidate.kind: candidate for candidate in result}
    assert dict(by_kind[MotivationKind.COMPLETION_STREAK].facts) == {"days": 3}
    assert dict(by_kind[MotivationKind.WEEKLY_PROGRESS].facts) == {
        "completed": 5,
        "days": 7,
    }


async def test_streak_must_be_current_and_templates_rotate_from_sent_history(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await add_user(session_factory)
    item_id = await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=2),
        item_type=ItemType.REFERENCE,
        processing=ProcessingStatus.QUEUED,
    )
    historical_start = now - timedelta(days=70)
    for offset in range(3):
        await add_event(
            session_factory,
            user_id,
            item_id,
            "DONE",
            historical_start + timedelta(days=offset),
        )

    for index in range(3):
        await add_item(
            session_factory,
            user_id,
            created_at=now - timedelta(days=1),
            priority=60,
            minutes=20,
            title=f"quick {index}",
        )
    before = await candidates(session_factory, user_id, now)
    quick = next(candidate for candidate in before if candidate.kind is MotivationKind.QUICK_WINS)
    assert quick.template_id == "quick_wins_v3"
    assert MotivationKind.COMPLETION_STREAK not in {candidate.kind for candidate in before}

    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=user_id,
                item_id=None,
                type="MOTIVATION_NUDGE",
                scheduled_at=now - timedelta(days=1),
                status="SENT",
                payload_json={"kind": "QUICK_WINS", "template_id": quick.template_id},
                sent_at=now - timedelta(days=1),
            )
        )
        await session.commit()
    rotated = await candidates(session_factory, user_id, now)
    rotated_quick = next(
        candidate for candidate in rotated if candidate.kind is MotivationKind.QUICK_WINS
    )
    assert rotated_quick.template_id == "quick_wins_v4"

    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=user_id,
                item_id=None,
                type="MOTIVATION_NUDGE",
                scheduled_at=now - timedelta(seconds=1),
                status="SENT",
                payload_json={"kind": "QUICK_WINS", "template_id": rotated_quick.template_id},
                sent_at=now - timedelta(hours=1),
            )
        )
        await session.commit()
    same_day = await candidates(session_factory, user_id, now)
    assert MotivationKind.QUICK_WINS not in {candidate.kind for candidate in same_day}


def test_motivation_templates_use_only_their_known_facts_and_rotate_in_order():
    """Keep maintained copy within deterministic facts and make every variant reachable."""
    fact_contracts = {
        MotivationKind.STALE_IMPORTANT: {"count": 4},
        MotivationKind.HIGH_INTEREST_STALE: {"count": 4},
        MotivationKind.QUICK_WINS: {"count": 4, "max_minutes": 20},
        MotivationKind.INBOX_GROWTH: {"created": 6, "resolved": 2, "net": 4},
        MotivationKind.COMPLETION_STREAK: {"days": 4},
        MotivationKind.WEEKLY_PROGRESS: {"completed": 5, "days": 7},
    }
    formatter = Formatter()
    for kind, templates in _TEMPLATES.items():
        assert len(templates) >= 3
        assert len({template.template_id for template in templates}) == len(templates)
        allowed = set(fact_contracts[kind])
        for template in templates:
            fields = {
                field_name
                for _literal, field_name, _format_spec, _conversion in formatter.parse(
                    template.text
                )
                if field_name is not None
            }
            assert fields <= allowed
            rendered = template.text.format(**fact_contracts[kind])
            assert len(rendered) <= 180
            assert all(str(fact_contracts[kind][field]) in rendered for field in fields)

        ids = [template.template_id for template in templates]
        assert _next_template(templates, None).template_id == ids[0]
        assert _next_template(templates, "quick_wins_v1").template_id == ids[0]
        for current, expected in zip(ids, ids[1:] + ids[:1], strict=True):
            assert _next_template(templates, current).template_id == expected


async def test_worker_sends_generic_claim_with_feedback_controls_and_send_attribution(
    session_factory,
):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory)
    await add_growth_items(session_factory, user_id, now)
    bot = RecordingBot()
    hooks = HookSpy()
    worker = ReminderWorker(session_factory, bot, max_send_attempts=1, attention_hook_service=hooks)

    assert await worker.process_once(now) == 1
    assert len(bot.messages) == 1
    chat_id, text, kwargs = bot.messages[0]
    assert chat_id == 42
    assert "3 новых" in text
    assert "+3" in text
    callbacks = {
        button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row
    }
    assert any(value.startswith("reminder:ok:") for value in callbacks)
    assert any(value.startswith("reminder:less:") for value in callbacks)
    assert hooks.calls == 0

    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert reminder.user_id == user_id
        assert reminder.item_id is None
        assert reminder.status == "SENT"
        assert reminder.sent_at == now
        assert reminder.scheduled_at == datetime(2026, 9, 24, 0, 0, 1)
        assert reminder.claim_generation == 1
        assert reminder.payload_json == {
            "kind": "INBOX_GROWTH",
            "facts": {"created": 3, "resolved": 0, "net": 3},
            "template_id": "inbox_growth_v3",
            "policy_level": 3,
            "local_date": "2026-09-24",
            "slot": 1,
        }
        sent_event = await session.scalar(
            select(Event).where(
                Event.reminder_id == reminder.id,
                Event.event_type == "REMINDER_SENT",
            )
        )
        assert sent_event.item_id is None
        assert sent_event.payload_json == {
            "reminder_type": "MOTIVATION_NUDGE",
            "policy_level": 3,
            "motivation_kind": "INBOX_GROWTH",
            "template_id": "inbox_growth_v3",
            "local_date": "2026-09-24",
            "slot": 1,
        }
        assert await session.scalar(select(func.count(Event.id))) == 1

    assert await worker.process_once(now + timedelta(minutes=1)) == 0
    assert len(bot.messages) == 1


async def test_disliked_nudge_kind_is_filtered_before_and_during_arbitration(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=5)
    stale_item_id = await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=45),
        priority=80,
        title="Old important item",
    )
    for index in range(3):
        await add_item(
            session_factory,
            user_id,
            created_at=now - timedelta(days=1),
            priority=60,
            minutes=20,
            title=f"Quick task {index}",
        )
    async with session_factory() as session:
        # The last successful Attention-family delivery makes PM-10 prefer a
        # nudge at level 5, so this exercises suppression before arbitration.
        session.add(
            Reminder(
                user_id=user_id,
                item_id=stale_item_id,
                type=PROACTIVE_ATTENTION,
                scheduled_at=now - timedelta(days=3),
                status="SENT",
                sent_at=now - timedelta(days=3),
                payload_json={"reminder_type": PROACTIVE_ATTENTION},
            )
        )
        disliked = Reminder(
            user_id=user_id,
            item_id=None,
            type=MOTIVATION_NUDGE,
            scheduled_at=now - timedelta(days=6),
            status="SENT",
            sent_at=now - timedelta(days=6),
            payload_json={
                "kind": "QUICK_WINS",
                "template_id": "quick_wins_v1",
                "policy_level": 5,
            },
        )
        session.add(disliked)
        await session.flush()
        session.add(
            Event(
                user_id=user_id,
                item_id=None,
                reminder_id=disliked.id,
                event_type="REMINDER_DISLIKED",
                payload_json={
                    "reminder_type": MOTIVATION_NUDGE,
                    "motivation_kind": "QUICK_WINS",
                    "template_id": "quick_wins_v1",
                    "policy_level": 5,
                },
                created_at=now - timedelta(days=6, hours=23, minutes=59),
            )
        )
        await session.commit()

    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot)
    assert await worker.process_once(now) == 1
    assert len(bot.messages) == 1
    assert "Важные сохранения старше месяца" in bot.messages[0][1]
    async with session_factory() as session:
        sent = await session.scalar(
            select(Reminder).where(
                Reminder.type == MOTIVATION_NUDGE,
                Reminder.status == "SENT",
                Reminder.sent_at == now,
            )
        )
        assert sent.payload_json["kind"] == "STALE_IMPORTANT"
        user = await session.get(User, user_id)
        assert user.settings_json["generic_motivation_enabled"] is True


@pytest.mark.parametrize(
    "level,attention,motivation",
    [(1, True, True), (3, False, True), (3, True, False)],
)
async def test_generic_nudge_requires_non_calm_attention_and_independent_opt_in(
    session_factory, level, attention, motivation
):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(
        session_factory, level=level, attention=attention, motivation=motivation
    )
    await add_growth_items(session_factory, user_id, now)
    bot = RecordingBot()

    assert await ReminderWorker(session_factory, bot).process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        assert await session.scalar(select(Reminder.id)) is None


async def test_failed_generic_delivery_reuses_slot_without_spending_budget(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=5)
    await add_growth_items(session_factory, user_id, now)

    failed_bot = RecordingBot(fail=True)
    assert (
        await ReminderWorker(session_factory, failed_bot, max_send_attempts=1).process_once(now)
        == 0
    )
    async with session_factory() as session:
        failed = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert failed.status == "FAILED"
        assert failed.sent_at is None
        assert failed.claim_generation == 1
        reminder_id = failed.id
        assert await session.scalar(select(Event.id)) is None

    succeeding_bot = RecordingBot()
    assert (
        await ReminderWorker(session_factory, succeeding_bot).process_once(
            now + timedelta(seconds=1)
        )
        == 1
    )
    async with session_factory() as session:
        sent = await session.get(Reminder, reminder_id)
        rows = (
            await session.scalars(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        ).all()
        assert len(rows) == 1
        assert sent.status == "SENT"
        assert sent.sent_at == now + timedelta(seconds=1)
        assert sent.claim_generation == 2
        assert sent.scheduled_at == datetime(2026, 9, 24, 0, 0, 1)
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.reminder_id == reminder_id,
                    Event.event_type == "REMINDER_SENT",
                )
            )
            == 1
        )


async def test_stale_generic_claim_recovers_same_slot_with_current_facts_and_fences_old_owner(
    session_factory, monkeypatch
):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory)
    await add_growth_items(session_factory, user_id, now, count=4)
    slot_at = datetime(2026, 9, 24, 0, 0, 1)
    async with session_factory() as session:
        reminder = Reminder(
            user_id=user_id,
            item_id=None,
            type=MOTIVATION_NUDGE,
            scheduled_at=slot_at,
            status="CLAIMED",
            claim_generation=4,
            claimed_at=now - timedelta(minutes=6),
            created_at=now - timedelta(minutes=6),
            payload_json={
                "kind": "INBOX_GROWTH",
                "facts": {"created": 99, "resolved": 0, "net": 99},
                "template_id": "inbox_growth_v1",
                "policy_level": 3,
                "local_date": "2026-09-24",
                "slot": 1,
            },
        )
        session.add(reminder)
        await session.commit()
        reminder_id = reminder.id
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: now)
    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(now) == 1
    assert "4 новых" in bot.messages[0][1]
    async with session_factory() as session:
        recovered = await session.get(Reminder, reminder_id)
        rows = (
            await session.scalars(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        ).all()
        assert len(rows) == 1
        assert recovered.status == "SENT"
        assert recovered.claim_generation == 5
        assert recovered.sent_at == now
        assert recovered.scheduled_at == slot_at
        assert recovered.payload_json["facts"] == {"created": 4, "resolved": 0, "net": 4}

    await worker._mark_failed(user_id, None, MOTIVATION_NUDGE, slot_at, 4)
    await worker._finalize_motivation_send(reminder_id, user_id, 4, now + timedelta(seconds=1))
    async with session_factory() as session:
        recovered = await session.get(Reminder, reminder_id)
        assert recovered.status == "SENT"
        assert recovered.claim_generation == 5
        assert recovered.sent_at == now


async def test_final_prepare_can_replace_a_quick_win_with_a_new_current_kind(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=5)
    quick_items = [
        await add_item(
            session_factory,
            user_id,
            created_at=now - timedelta(days=1),
            priority=60,
            minutes=20,
            title=f"quick {index}",
        )
        for index in range(3)
    ]
    stale_soon_id = await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=30) + timedelta(seconds=1),
        priority=75,
        title="stale soon",
    )
    async with session_factory() as session:
        for item_id in [*quick_items, stale_soon_id]:
            session.add(
                Reminder(
                    user_id=user_id,
                    item_id=item_id,
                    type=PROACTIVE_ATTENTION,
                    scheduled_at=now - timedelta(hours=4),
                    status="SENT",
                    sent_at=now - timedelta(hours=4),
                )
            )
        await session.commit()

    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot)
    prepare = worker._prepare_motivation_send

    async def cross_stale_threshold_before_prepare(
        reminder_id, current_user_id, generation, current_now
    ):
        async with session_factory() as session:
            item = await session.get(Item, stale_soon_id)
            item.created_at = now - timedelta(days=30)
            await session.commit()
        return await prepare(reminder_id, current_user_id, generation, current_now)

    worker._prepare_motivation_send = cross_stale_threshold_before_prepare
    assert await worker.process_once(now) == 1
    assert "Важные сохранения старше месяца" in bot.messages[0][1]
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert reminder.payload_json["kind"] == "STALE_IMPORTANT"
        assert reminder.payload_json["facts"] == {"count": 1}


@pytest.mark.parametrize("setting", ["attention_enabled", "generic_motivation_enabled"])
async def test_preparation_rechecks_both_opt_out_settings(session_factory, setting):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory)
    await add_growth_items(session_factory, user_id, now)
    worker = ReminderWorker(session_factory, RecordingBot())
    prepare = worker._prepare_motivation_send

    async def turn_setting_off_before_prepare(
        reminder_id, current_user_id, generation, current_now
    ):
        async with session_factory() as session:
            user = await session.get(User, user_id)
            values = dict(user.settings_json)
            values[setting] = False
            user.settings_json = values
            await session.commit()
        return await prepare(reminder_id, current_user_id, generation, current_now)

    worker._prepare_motivation_send = turn_setting_off_before_prepare
    assert await worker.process_once(now) == 0
    assert worker.bot.messages == []
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert reminder.status == "CANCELLED"


async def test_level_five_allows_two_generic_kinds_but_not_a_third_in_one_local_day(
    session_factory,
):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=5)
    await add_growth_items(session_factory, user_id, now)
    event_item_id = await add_item(
        session_factory,
        user_id,
        created_at=now - timedelta(days=40),
        item_type=ItemType.REFERENCE,
        processing=ProcessingStatus.QUEUED,
    )
    for offset in range(5):
        await add_event(
            session_factory,
            user_id,
            event_item_id,
            "DONE",
            now - timedelta(days=offset),
        )
    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(now) == 1
    async with session_factory() as session:
        first = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert first.payload_json["kind"] == "COMPLETION_STREAK"

    await add_proactive_item(session_factory, user_id, now, title="Intervening Item")
    assert await worker.process_once(now + timedelta(hours=2)) == 1
    async with session_factory() as session:
        proactive = await session.scalar(
            select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION)
        )
        assert proactive.status == "SENT"

    assert await worker.process_once(now + timedelta(hours=4)) == 1
    async with session_factory() as session:
        nudges = (
            await session.scalars(
                select(Reminder)
                .where(Reminder.type == MOTIVATION_NUDGE, Reminder.status == "SENT")
                .order_by(Reminder.sent_at, Reminder.id)
            )
        ).all()
        assert [row.payload_json["kind"] for row in nudges] == [
            "COMPLETION_STREAK",
            "STALE_IMPORTANT",
        ]

        session.add(
            Reminder(
                user_id=user_id,
                item_id=None,
                type="DAILY_DIGEST",
                scheduled_at=datetime(2026, 9, 24),
                status="SENT",
                sent_at=now + timedelta(hours=4, minutes=30),
            )
        )
        await session.commit()
    assert await worker.process_once(now + timedelta(hours=6)) == 0
    assert len(bot.messages) == 3
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.type == MOTIVATION_NUDGE,
                    Reminder.status == "SENT",
                )
            )
            == 2
        )


async def test_generic_intent_blocks_concurrent_worker_until_delivery_finishes(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory)
    await add_growth_items(session_factory, user_id, now)
    first_bot, second_bot = BlockingBot(), RecordingBot()
    first = asyncio.create_task(
        ReminderWorker(session_factory, first_bot, max_send_attempts=1).process_once(now)
    )
    await asyncio.wait_for(first_bot.started.wait(), timeout=1)

    assert (
        await ReminderWorker(session_factory, second_bot).process_once(now + timedelta(seconds=1))
        == 0
    )
    assert second_bot.messages == []

    first_bot.release.set()
    assert await asyncio.wait_for(first, timeout=1) == 1
    async with session_factory() as session:
        rows = (
            await session.scalars(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "SENT"


async def test_preparation_recomputes_and_cancels_disappeared_quick_wins(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=5)
    items = [
        await add_item(
            session_factory,
            user_id,
            created_at=now - timedelta(days=1),
            priority=60,
            minutes=20,
            title=f"quick {index}",
        )
        for index in range(3)
    ]
    async with session_factory() as session:
        for item_id in items:
            session.add(
                Reminder(
                    user_id=user_id,
                    item_id=item_id,
                    type=PROACTIVE_ATTENTION,
                    scheduled_at=now - timedelta(hours=4),
                    status="SENT",
                    sent_at=now - timedelta(hours=4),
                )
            )
        await session.commit()
    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot, max_send_attempts=1)
    prepare = worker._prepare_motivation_send

    async def complete_one_before_final_prepare(
        reminder_id, current_user_id, generation, current_now
    ):
        async with session_factory() as session:
            item = await session.get(Item, items[0])
            item.state = ItemState.DONE
            await session.commit()
        return await prepare(reminder_id, current_user_id, generation, current_now)

    worker._prepare_motivation_send = complete_one_before_final_prepare
    assert await worker.process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert reminder.status == "CANCELLED"
        assert reminder.payload_json["kind"] == "QUICK_WINS"


async def test_proactive_wins_at_normal_then_level_four_alternates_when_both_exist(
    session_factory,
):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=4)
    await add_growth_items(session_factory, user_id, now)
    first_item = await add_proactive_item(
        session_factory, user_id, now, title="First proactive Item"
    )
    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot)

    assert await worker.process_once(now) == 1
    async with session_factory() as session:
        latest = await session.scalar(select(Reminder).where(Reminder.type == PROACTIVE_ATTENTION))
        assert latest.item_id == first_item

    second_item = await add_proactive_item(
        session_factory, user_id, now, title="Second proactive Item"
    )
    assert await worker.process_once(now + timedelta(hours=3)) == 1
    async with session_factory() as session:
        sent_types = list(
            (
                await session.scalars(
                    select(Reminder.type)
                    .where(Reminder.status == "SENT")
                    .order_by(Reminder.sent_at, Reminder.id)
                )
            ).all()
        )
        assert sent_types == [PROACTIVE_ATTENTION, MOTIVATION_NUDGE]

    assert await worker.process_once(now + timedelta(hours=6)) == 1
    async with session_factory() as session:
        proactive = (
            await session.scalars(
                select(Reminder)
                .where(Reminder.type == PROACTIVE_ATTENTION, Reminder.status == "SENT")
                .order_by(Reminder.sent_at, Reminder.id)
            )
        ).all()
        assert [reminder.item_id for reminder in proactive] == [first_item, second_item]
        assert len(bot.messages) == 3
        assert "INBOX_GROWTH" not in bot.messages[0][1]


async def test_proactive_wins_levels_one_through_three_and_failure_has_no_generic_fallback(
    session_factory,
):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=3)
    await add_growth_items(session_factory, user_id, now)
    await add_proactive_item(session_factory, user_id, now, title="Proactive wins")

    bot = RecordingBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(select(Reminder.id).where(Reminder.type == MOTIVATION_NUDGE))
            is None
        )
        assert (
            await session.scalar(
                select(Reminder.status).where(Reminder.type == PROACTIVE_ATTENTION)
            )
            == "SENT"
        )

    second_user_id = await make_worker_user(session_factory, level=3, telegram_id=1000)
    await add_growth_items(session_factory, second_user_id, now)
    await add_proactive_item(session_factory, second_user_id, now, title="Failed proactive")
    failing_bot = RecordingBot(fail=True)
    assert (
        await ReminderWorker(session_factory, failing_bot, max_send_attempts=1).process_once(now)
        == 0
    )
    async with session_factory() as session:
        failed = await session.scalar(
            select(Reminder).where(
                Reminder.user_id == second_user_id,
                Reminder.type == PROACTIVE_ATTENTION,
            )
        )
        assert failed.status == "FAILED"
        assert (
            await session.scalar(
                select(Reminder.id).where(
                    Reminder.user_id == second_user_id,
                    Reminder.type == MOTIVATION_NUDGE,
                )
            )
            is None
        )


async def test_generic_shares_total_budget_and_snooze_minimum_gap(session_factory):
    now = datetime(2026, 9, 24, 12)
    user_id = await make_worker_user(session_factory, level=3)
    await add_growth_items(session_factory, user_id, now)
    item_id = await add_proactive_item(session_factory, user_id, now, title="Budget item")
    async with session_factory() as session:
        for type_, item, scheduled in (
            ("DAILY_DIGEST", None, datetime(2026, 9, 24)),
            (PROACTIVE_ATTENTION, item_id, now - timedelta(hours=3)),
            (MOTIVATION_NUDGE, None, datetime(2026, 9, 24, 0, 0, 1)),
        ):
            session.add(
                Reminder(
                    user_id=user_id,
                    item_id=item,
                    type=type_,
                    scheduled_at=scheduled,
                    status="SENT",
                    sent_at=now - timedelta(hours=3),
                    payload_json=(
                        {"kind": "INBOX_GROWTH", "template_id": "inbox_growth_v1"}
                        if type_ == MOTIVATION_NUDGE
                        else None
                    ),
                )
            )
        await session.commit()

    bot = RecordingBot()
    assert await ReminderWorker(session_factory, bot).process_once(now) == 0
    assert bot.messages == []
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.status == "SENT",
                    Reminder.type.in_(("DAILY_DIGEST", PROACTIVE_ATTENTION, MOTIVATION_NUDGE)),
                )
            )
            == 3
        )

    second_user_id = await make_worker_user(session_factory, level=3, telegram_id=1000)
    await add_growth_items(session_factory, second_user_id, now)
    snoozed_item_id = await add_item(
        session_factory,
        second_user_id,
        created_at=now - timedelta(days=1),
        item_type=ItemType.REFERENCE,
        processing=ProcessingStatus.READY,
        state=ItemState.SNOOZED,
    )
    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=second_user_id,
                item_id=snoozed_item_id,
                type="SNOOZE_RESURFACE",
                scheduled_at=now - timedelta(hours=1),
                status="SENT",
                sent_at=now - timedelta(hours=1),
            )
        )
        await session.commit()
    snooze_bot = RecordingBot()
    assert await ReminderWorker(session_factory, snooze_bot).process_once(now) == 0
    assert snooze_bot.messages == []


async def test_worker_uses_the_original_cycle_time_only_when_explicitly_supplied(
    session_factory, monkeypatch
):
    user_id = await make_worker_user(session_factory)
    base = datetime(2026, 9, 24, 18, 59, 30)  # 21:59:30 Europe/Moscow
    await add_growth_items(session_factory, user_id, base)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        user.timezone = "Europe/Moscow"
        settings = dict(user.settings_json)
        settings["quiet_hours_start"] = "22:00"
        settings["quiet_hours_end"] = "08:00"
        user.settings_json = settings
        await session.commit()
    clock = [base]
    monkeypatch.setattr("app.services.notifications._utc_now", lambda: clock[0])
    bot = RecordingBot()
    worker = ReminderWorker(session_factory, bot, max_send_attempts=1)
    prepare = worker._prepare_motivation_send

    async def cross_quiet_boundary_before_prepare(reminder_id, current_user_id, generation, now):
        clock[0] += timedelta(minutes=1)
        return await prepare(reminder_id, current_user_id, generation, None)

    worker._prepare_motivation_send = cross_quiet_boundary_before_prepare
    assert await worker.process_once() == 0
    assert bot.messages == []
    async with session_factory() as session:
        reminder = await session.scalar(select(Reminder).where(Reminder.type == MOTIVATION_NUDGE))
        assert reminder.status == "CLAIMED"
