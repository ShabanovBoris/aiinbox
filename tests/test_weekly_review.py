from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.bot.formatting import format_weekly_review
from app.bot.handlers import on_weekly
from app.bot.navigation import BOT_COMMANDS
from app.config import Settings
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.calendar_windows import local_dates_utc_window
from app.services.weekly_review import (
    CategoryCount,
    WeeklyBacklog,
    WeeklyFlow,
    WeeklyRecommendation,
    WeeklyReminderOutcomes,
    WeeklyReview,
    WeeklyReviewService,
)
from app.storage.models import Event, Item, Reminder, User

_NOW = datetime(2026, 3, 30, 10, 0, tzinfo=UTC)
_ZONE = ZoneInfo("Europe/Helsinki")
_SERVICE = WeeklyReviewService()


async def _user(session, telegram_user_id: int = 42, *, timezone: str = "Europe/Helsinki") -> User:
    user = User(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_user_id,
        timezone=timezone,
    )
    session.add(user)
    await session.flush()
    return user


async def _item(
    session,
    user_id: int,
    *,
    title: str = "Item",
    category: str | None = "AI",
    created_at: datetime = _NOW - timedelta(days=1),
    priority: int | None = 50,
    interest: int = 2,
    item_type: ItemType = ItemType.ACTION,
    state: ItemState = ItemState.ACTIVE,
    status: ProcessingStatus = ProcessingStatus.READY,
    minutes: int | None = None,
) -> Item:
    created = created_at.astimezone(UTC).replace(tzinfo=None) if created_at.tzinfo else created_at
    item = Item(
        user_id=user_id,
        telegram_message_id=None,
        source_index=0,
        processing_status=status,
        state=state,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="",
        title=title,
        category=category,
        item_type=item_type,
        priority_score=priority,
        interest_level=interest,
        estimated_action_minutes=minutes,
        created_at=created,
    )
    session.add(item)
    await session.flush()
    return item


async def _event(
    session,
    user_id: int,
    event_type: str,
    created_at: datetime,
    *,
    item_id: int | None = None,
    reminder_id: int | None = None,
    payload: dict | None = None,
) -> Event:
    created = created_at.astimezone(UTC).replace(tzinfo=None) if created_at.tzinfo else created_at
    event = Event(
        user_id=user_id,
        item_id=item_id,
        reminder_id=reminder_id,
        event_type=event_type,
        payload_json=payload,
        created_at=created,
    )
    session.add(event)
    await session.flush()
    return event


async def _reminder_event(
    session,
    user_id: int,
    item_id: int | None,
    event_type: str,
    created_at: datetime,
    *,
    reminder_type: str = "PROACTIVE_ATTENTION",
    status: str = "SENT",
    schedule_index: int = 0,
) -> tuple[Reminder, Event]:
    created = created_at.astimezone(UTC).replace(tzinfo=None) if created_at.tzinfo else created_at
    payload = {"reminder_type": reminder_type}
    reminder = Reminder(
        user_id=user_id,
        item_id=item_id,
        type=reminder_type,
        scheduled_at=created + timedelta(days=500, seconds=schedule_index),
        status=status,
        sent_at=created if status == "SENT" else None,
        payload_json=payload,
    )
    session.add(reminder)
    await session.flush()
    event = await _event(
        session,
        user_id,
        event_type,
        created,
        item_id=item_id,
        reminder_id=reminder.id,
        payload=payload,
    )
    return reminder, event


async def _build(session, user_id: int, *, now: datetime = _NOW) -> WeeklyReview:
    return await _SERVICE.build(session, user_id, zone=_ZONE, now=now)


@pytest.mark.asyncio
async def test_week_window_uses_seven_helsinki_dates_and_half_open_dst_bounds(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        other = await _user(session, 1000)
        today = _NOW.astimezone(_ZONE).date()
        start, end = local_dates_utc_window(
            today - timedelta(days=6), today + timedelta(days=1), _ZONE
        )
        assert end - start == timedelta(hours=167)

        first = await _item(session, user.id, created_at=start)
        await _item(session, user.id, created_at=start - timedelta(seconds=1))
        await _item(session, user.id, created_at=end)
        await _item(session, other.id, created_at=start)
        await _event(session, user.id, "DONE", start, item_id=first.id)
        end_item = await _item(session, user.id, created_at=start - timedelta(days=2))
        await _event(session, user.id, "DONE", end, item_id=end_item.id)
        await _event(session, other.id, "DONE", start, item_id=first.id)
        await session.commit()

        review = await _build(session, user.id)

    assert review.flow.created == 1
    assert review.flow.completed == 1
    assert review.flow.net_change == 0


@pytest.mark.asyncio
async def test_week_window_keeps_the_25_hour_autumn_dst_day(session_factory):
    fall_now = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        user = await _user(session)
        today = fall_now.astimezone(_ZONE).date()
        start, end = local_dates_utc_window(
            today - timedelta(days=6), today + timedelta(days=1), _ZONE
        )
        assert end - start == timedelta(hours=169)
        await _item(session, user.id, created_at=start)
        await session.commit()

        review = await _SERVICE.build(session, user.id, zone=_ZONE, now=fall_now)

    assert review.flow.created == 1


@pytest.mark.asyncio
async def test_flow_uses_lifecycle_events_and_keeps_negative_net_change(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        created_items = [await _item(session, user.id, title=f"new-{i}") for i in range(3)]
        for index in range(8):
            item = await _item(
                session,
                user.id,
                title=f"done-{index}",
                created_at=_NOW - timedelta(days=20),
                state=ItemState.DONE,
            )
            await _event(session, user.id, "DONE", _NOW - timedelta(hours=1), item_id=item.id)
        for index in range(3):
            item = await _item(
                session,
                user.id,
                title=f"archived-{index}",
                created_at=_NOW - timedelta(days=20),
                state=ItemState.ARCHIVED,
            )
            await _event(session, user.id, "ARCHIVED", _NOW - timedelta(hours=1), item_id=item.id)
        reminder, _ = await _reminder_event(
            session,
            user.id,
            created_items[0].id,
            "REMINDER_DONE",
            _NOW - timedelta(hours=1),
        )
        assert reminder.type == "PROACTIVE_ATTENTION"
        await session.commit()

        review = await _build(session, user.id)
        assert review.reminder_outcomes is not None
        assert review.reminder_outcomes.done == 1
        assert review.reminder_outcomes.opened == 0

    assert review.flow.created == 3
    assert review.flow.completed == 8
    assert review.flow.archived == 3
    assert review.flow.net_change == -8


@pytest.mark.asyncio
async def test_backlog_metrics_use_ready_active_actionable_and_exact_thresholds(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        await _item(
            session,
            user.id,
            title="Boundary 74",
            created_at=_NOW - timedelta(days=30),
            priority=74,
        )
        await _item(
            session,
            user.id,
            title="Boundary 75",
            created_at=_NOW - timedelta(days=30),
            priority=75,
            interest=3,
        )
        await _item(
            session,
            user.id,
            title="One second younger",
            created_at=_NOW - timedelta(days=30) + timedelta(seconds=1),
            priority=90,
        )
        await _item(
            session,
            user.id,
            title="Malformed future",
            created_at=_NOW + timedelta(seconds=1),
            priority=90,
        )
        excluded = [
            (ItemState.SNOOZED, ProcessingStatus.READY, ItemType.ACTION),
            (ItemState.DONE, ProcessingStatus.READY, ItemType.ACTION),
            (ItemState.ARCHIVED, ProcessingStatus.READY, ItemType.ACTION),
            (ItemState.ACTIVE, ProcessingStatus.FAILED, ItemType.ACTION),
            (ItemState.ACTIVE, ProcessingStatus.PROCESSING, ItemType.ACTION),
            (ItemState.ACTIVE, ProcessingStatus.QUEUED, ItemType.ACTION),
            (ItemState.ACTIVE, ProcessingStatus.READY, ItemType.REFERENCE),
            (ItemState.ACTIVE, ProcessingStatus.READY, ItemType.IDEA),
            (ItemState.ACTIVE, ProcessingStatus.READY, ItemType.SOMEDAY),
        ]
        for state, status, item_type in excluded:
            await _item(
                session,
                user.id,
                title=f"excluded-{state.value}-{status.value}-{item_type.value}",
                created_at=_NOW - timedelta(days=60),
                priority=99,
                state=state,
                status=status,
                item_type=item_type,
            )
        await session.commit()

        review = await _build(session, user.id)

    assert review.backlog.active_actionable == 4
    assert review.backlog.high_priority == 3
    assert review.backlog.high_interest == 1
    assert review.backlog.stale == 2
    assert review.backlog.old_important_unrevisited == 1


@pytest.mark.parametrize("event_type", ["TODAY_SHOWN", "ATTENTION_SHOWN"])
@pytest.mark.parametrize(
    ("age", "expected"),
    [(timedelta(days=14), 0), (timedelta(days=14, seconds=1), 1)],
)
@pytest.mark.asyncio
async def test_old_important_revisit_event_has_inclusive_14_day_boundary(
    session_factory, event_type, age, expected
):
    async with session_factory() as session:
        user = await _user(session)
        item = await _item(
            session,
            user.id,
            created_at=_NOW - timedelta(days=40),
            priority=80,
        )
        await _event(session, user.id, event_type, _NOW - age, item_id=item.id)
        await session.commit()

        review = await _build(session, user.id)

    assert review.backlog.old_important_unrevisited == expected


@pytest.mark.parametrize(
    ("reminder_type", "status", "expected"),
    [
        ("PROACTIVE_ATTENTION", "SENT", 0),
        ("PROACTIVE_ATTENTION", "FAILED", 1),
        ("MOTIVATION_NUDGE", "SENT", 1),
    ],
)
@pytest.mark.asyncio
async def test_only_successful_proactive_reminder_counts_as_old_item_revisit(
    session_factory, reminder_type, status, expected
):
    async with session_factory() as session:
        user = await _user(session)
        item = await _item(
            session,
            user.id,
            created_at=_NOW - timedelta(days=40),
            priority=80,
        )
        reminder = Reminder(
            user_id=user.id,
            item_id=item.id,
            type=reminder_type,
            scheduled_at=_NOW.replace(tzinfo=None) - timedelta(days=5),
            status=status,
            sent_at=_NOW.replace(tzinfo=None) - timedelta(days=5) if status == "SENT" else None,
        )
        session.add(reminder)
        await session.commit()

        review = await _build(session, user.id)

    assert review.backlog.old_important_unrevisited == expected


@pytest.mark.asyncio
async def test_category_aggregates_and_insights_use_current_category_and_thresholds(
    session_factory,
):
    async with session_factory() as session:
        user = await _user(session)
        items_by_category: dict[str, list[Item]] = {}
        for category, count in (("AI", 4), ("Piano", 3), ("Android", 2), ("Finance", 1)):
            items_by_category[category] = [
                await _item(session, user.id, category=category, created_at=_NOW)
                for _ in range(count)
            ]

        corrected = items_by_category["AI"][0]
        corrected.category = "Current AI"
        for category, count in (("AI", 3), ("Piano", 1), ("Android", 1)):
            for item in items_by_category[category][:count]:
                await _event(session, user.id, "DONE", _NOW, item_id=item.id)
        for category in ("Piano", "Piano", "AI"):
            await _event(
                session, user.id, "SNOOZED", _NOW, item_id=items_by_category[category][0].id
            )
        await _item(session, user.id, title="Blank category", category="   ", created_at=_NOW)
        await _item(session, user.id, title="No category", category=None, created_at=_NOW)
        await session.commit()

        review = await _build(session, user.id)

    assert review.created_categories == (
        CategoryCount("AI", 3),
        CategoryCount("Piano", 3),
        CategoryCount("Android", 2),
    )
    assert review.completed_categories == (
        CategoryCount("AI", 2),
        CategoryCount("Android", 1),
        CategoryCount("Current AI", 1),
    )
    assert review.most_postponed == CategoryCount("Piano", 2)
    assert review.strongest_progress == CategoryCount("AI", 2)


@pytest.mark.asyncio
async def test_category_ties_are_alphabetical_and_single_events_do_not_create_insights(
    session_factory,
):
    async with session_factory() as session:
        user = await _user(session)
        for category in ("Piano", "AI"):
            item = await _item(session, user.id, category=category, created_at=_NOW)
            await _event(session, user.id, "SNOOZED", _NOW, item_id=item.id)
            await _event(session, user.id, "DONE", _NOW, item_id=item.id)
        await session.commit()

        review = await _build(session, user.id)

    assert review.completed_categories == (CategoryCount("AI", 1), CategoryCount("Piano", 1))
    assert review.most_postponed is None
    assert review.strongest_progress is None


@pytest.mark.asyncio
async def test_attention_reminder_outcomes_are_raw_and_exclude_other_types(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        other = await _user(session, 1000)
        item = await _item(session, user.id)
        event_types = [
            *("REMINDER_SENT" for _ in range(4)),
            *("REMINDER_OPENED" for _ in range(2)),
            "REMINDER_SNOOZED",
            "REMINDER_DONE",
            "REMINDER_DISMISSED",
            "REMINDER_DISLIKED",
        ]
        for index, event_type in enumerate(event_types):
            await _reminder_event(
                session,
                user.id,
                item.id,
                event_type,
                _NOW - timedelta(hours=1),
                schedule_index=index,
            )
        for index, reminder_type in enumerate(
            ("MOTIVATION_NUDGE", "DAILY_DIGEST", "SNOOZE_RESURFACE"), start=20
        ):
            await _reminder_event(
                session,
                user.id,
                None,
                "REMINDER_SENT",
                _NOW - timedelta(hours=1),
                reminder_type=reminder_type,
                schedule_index=index,
            )
        await _reminder_event(
            session,
            other.id,
            None,
            "REMINDER_SENT",
            _NOW - timedelta(hours=1),
            schedule_index=30,
        )
        await session.commit()

        review = await _build(session, user.id)

    assert review.reminder_outcomes == WeeklyReminderOutcomes(
        sent=4,
        opened=2,
        snoozed=1,
        done=1,
        dismissed=1,
        disliked=1,
    )


@pytest.mark.asyncio
async def test_recommendations_are_ranked_distinct_and_cleanup_accepts_non_actionable(
    session_factory,
):
    async with session_factory() as session:
        user = await _user(session)
        old = await _item(
            session,
            user.id,
            title="Old important quick step",
            created_at=_NOW - timedelta(days=40),
            priority=80,
            minutes=15,
        )
        quick = await _item(
            session,
            user.id,
            title="Next quick step",
            created_at=_NOW - timedelta(days=2),
            priority=60,
            minutes=20,
        )
        cleanup = await _item(
            session,
            user.id,
            title="Old reference",
            created_at=_NOW - timedelta(days=90),
            priority=74,
            interest=2,
            item_type=ItemType.REFERENCE,
        )
        await _event(
            session,
            user.id,
            "NOT_INTERESTING",
            _NOW - timedelta(days=200),
            item_id=cleanup.id,
        )
        await _item(
            session,
            user.id,
            title="One second too young",
            created_at=_NOW - timedelta(days=90) + timedelta(seconds=1),
            priority=20,
            interest=1,
            item_type=ItemType.IDEA,
        )
        await _item(
            session,
            user.id,
            title="High priority cannot cleanup",
            created_at=_NOW - timedelta(days=100),
            priority=75,
            interest=1,
            item_type=ItemType.SOMEDAY,
        )
        await _item(
            session,
            user.id,
            title="Failed cannot cleanup",
            created_at=_NOW - timedelta(days=100),
            priority=10,
            interest=1,
            status=ProcessingStatus.FAILED,
        )
        await session.commit()

        review = await _build(session, user.id)

    assert [recommendation.kind for recommendation in review.recommendations] == [
        "RETURN_OLD_IMPORTANT",
        "QUICK_WIN",
        "CLEANUP_REVIEW",
    ]
    assert review.recommendations[0].item_id == old.id
    assert review.recommendations[1].item_id == quick.id
    assert review.recommendations[0].item_id != review.recommendations[1].item_id
    assert review.recommendations[2].item_id == cleanup.id
    assert len({recommendation.item_id for recommendation in review.recommendations}) == 3


@pytest.mark.asyncio
async def test_cleanup_accepts_interest_one_without_event_for_any_active_item_type(
    session_factory,
):
    async with session_factory() as session:
        user = await _user(session)
        cleanup = await _item(
            session,
            user.id,
            title="Someday idea",
            created_at=_NOW - timedelta(days=90),
            priority=74,
            interest=1,
            item_type=ItemType.SOMEDAY,
        )
        await session.commit()

        review = await _build(session, user.id)

    assert len(review.recommendations) == 1
    assert review.recommendations[0].kind == "CLEANUP_REVIEW"
    assert review.recommendations[0].item_id == cleanup.id


@pytest.mark.asyncio
async def test_quick_win_boundaries_and_revisited_old_item_recommendation(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        revisited = await _item(
            session,
            user.id,
            title="Revisited old",
            created_at=_NOW - timedelta(days=30),
            priority=75,
            minutes=21,
        )
        await _event(
            session, user.id, "TODAY_SHOWN", _NOW - timedelta(days=1), item_id=revisited.id
        )
        await _item(session, user.id, title="Twenty minutes", priority=60, minutes=20)
        await _item(session, user.id, title="Twenty one minutes", priority=80, minutes=21)
        await _item(session, user.id, title="Priority 59", priority=59, minutes=10)
        await session.commit()

        review = await _build(session, user.id)

    assert review.recommendations[0].item_id == revisited.id
    assert review.recommendations[0].kind == "RETURN_OLD_IMPORTANT"
    quick_wins = [r for r in review.recommendations if r.kind == "QUICK_WIN"]
    assert len(quick_wins) == 1
    assert quick_wins[0].estimated_action_minutes == 20


@pytest.mark.parametrize(
    ("minutes", "priority", "eligible"),
    [(20, 60, True), (21, 80, False), (20, 59, False), (None, 60, False)],
)
@pytest.mark.asyncio
async def test_quick_win_uses_inclusive_minutes_and_priority_floor(
    session_factory, minutes, priority, eligible
):
    async with session_factory() as session:
        user = await _user(session)
        item = await _item(
            session,
            user.id,
            created_at=_NOW - timedelta(days=5),
            priority=priority,
            minutes=minutes,
        )
        await session.commit()

        review = await _build(session, user.id)

    quick_wins = [r for r in review.recommendations if r.kind == "QUICK_WIN"]
    assert (len(quick_wins) == 1) is eligible
    if eligible:
        assert quick_wins[0].item_id == item.id


@pytest.mark.asyncio
async def test_old_important_recommendation_respects_pm07_reminder_penalty(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        disliked = await _item(
            session,
            user.id,
            title="Disliked category",
            category="AI",
            created_at=_NOW - timedelta(days=40),
            priority=80,
        )
        preferred = await _item(
            session,
            user.id,
            title="Other category",
            category="Piano",
            created_at=_NOW - timedelta(days=40),
            priority=80,
        )
        payload = {
            "reminder_type": "PROACTIVE_ATTENTION",
            "category": "AI",
            "item_type": "ACTION",
        }
        reminder = Reminder(
            user_id=user.id,
            item_id=disliked.id,
            type="PROACTIVE_ATTENTION",
            scheduled_at=_NOW.replace(tzinfo=None) - timedelta(days=2),
            status="SENT",
            sent_at=_NOW.replace(tzinfo=None) - timedelta(days=2),
            payload_json=payload,
        )
        session.add(reminder)
        await session.flush()
        await _event(
            session,
            user.id,
            "REMINDER_DISLIKED",
            _NOW - timedelta(days=1),
            item_id=disliked.id,
            reminder_id=reminder.id,
            payload=payload,
        )
        await session.commit()

        review = await _build(session, user.id)

    assert review.recommendations[0].kind == "RETURN_OLD_IMPORTANT"
    assert review.recommendations[0].item_id == preferred.id


@pytest.mark.asyncio
async def test_weekly_service_is_read_only_and_scoped_to_one_user(session_factory):
    async with session_factory() as session:
        user = await _user(session)
        other = await _user(session, 1000)
        item = await _item(
            session,
            user.id,
            title="Canonical item",
            created_at=_NOW - timedelta(days=40),
            priority=80,
            interest=3,
        )
        foreign_item = await _item(session, other.id, title="Foreign item", priority=99)
        await _event(session, user.id, "DONE", _NOW - timedelta(days=1), item_id=item.id)
        await _event(session, other.id, "DONE", _NOW - timedelta(days=1), item_id=foreign_item.id)
        await _reminder_event(
            session,
            user.id,
            item.id,
            "REMINDER_SENT",
            _NOW - timedelta(days=1),
            schedule_index=50,
        )
        await session.commit()
        before = (
            await session.scalar(select(func.count()).select_from(Item)),
            await session.scalar(select(func.count()).select_from(Event)),
            await session.scalar(select(func.count()).select_from(Reminder)),
            item.state,
            item.priority_score,
            item.interest_level,
        )

        review = await _build(session, user.id)
        await session.flush()
        after = (
            await session.scalar(select(func.count()).select_from(Item)),
            await session.scalar(select(func.count()).select_from(Event)),
            await session.scalar(select(func.count()).select_from(Reminder)),
            item.state,
            item.priority_score,
            item.interest_level,
        )

    assert review.flow.created == 0
    assert review.flow.completed == 1
    assert review.reminder_outcomes is not None
    assert review.reminder_outcomes.sent == 1
    assert before == after


def test_weekly_formatter_omits_empty_sections_and_bounds_dynamic_labels():
    empty = WeeklyReview(
        flow=WeeklyFlow(0, 0, 0, 0),
        backlog=WeeklyBacklog(0, 0, 0, 0, 0),
        created_categories=(),
        completed_categories=(),
        most_postponed=None,
        strongest_progress=None,
        reminder_outcomes=None,
        recommendations=(),
    )
    assert "недостаточно данных" in format_weekly_review(empty).lower()
    assert "Attention:" not in format_weekly_review(empty)

    populated = WeeklyReview(
        flow=WeeklyFlow(1, 0, 0, 1),
        backlog=WeeklyBacklog(1, 0, 0, 0, 0),
        created_categories=(CategoryCount("C" * 300, 1),),
        completed_categories=(),
        most_postponed=None,
        strongest_progress=None,
        reminder_outcomes=WeeklyReminderOutcomes(1, 0, 0, 0, 0, 0),
        recommendations=(
            WeeklyRecommendation("RETURN_OLD_IMPORTANT", 1, "T" * 1000),
            WeeklyRecommendation("QUICK_WIN", 2, "Q" * 1000, 20),
            WeeklyRecommendation("CLEANUP_REVIEW", 3, "R" * 1000),
        ),
    )
    text = format_weekly_review(populated)
    assert len(text) <= 4096
    assert "Добавлено: 1" in text
    assert "На следующую неделю:" in text
    assert "Attention:" in text
    assert "Чаще добавлял:" in text
    assert "Проверить актуальность:" in text
    assert "Готово: 0" not in text
    assert "открыто 0" not in text


class _Message:
    def __init__(self, user_id: int):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)
        self.responses: list[str] = []
        self.reply_markups = []

    async def answer(self, text: str, **kwargs):
        self.responses.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))


@pytest.mark.asyncio
async def test_weekly_handler_sends_one_message_and_falls_back_from_invalid_user_timezone(
    settings, session_factory
):
    async with session_factory() as session:
        user = await _user(session, timezone="Not/A_Zone")
        await _item(session, user.id, title="Current task")
        quick_win = await _item(
            session,
            user.id,
            title="Quick step",
            created_at=datetime.now(UTC) - timedelta(days=1),
            priority=90,
            minutes=10,
        )
        quick_win_id = quick_win.id
        await session.commit()

    message = _Message(42)
    await on_weekly(message, settings, session_factory)

    assert len(message.responses) == 1
    assert "Неделя" in message.responses[0]
    assert "Активных actionable: 2" in message.responses[0]
    assert "Быстрый шаг: Quick step" in message.responses[0]
    buttons = [button for row in message.reply_markups[0].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [f"item:view:{quick_win_id}"]
    assert [button.text for button in buttons] == ["Quick step"]
    assert any(command.command == "weekly" for command in BOT_COMMANDS)


@pytest.mark.asyncio
async def test_weekly_handler_ignores_unauthorized_user_without_writes(session_factory):
    settings = Settings(
        _env_file=None,
        telegram_bot_token="",
        allowed_telegram_user_ids="1000",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    message = _Message(42)

    await on_weekly(message, settings, session_factory)

    assert message.responses == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert await session.scalar(select(func.count()).select_from(Item)) == 0
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
        assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
