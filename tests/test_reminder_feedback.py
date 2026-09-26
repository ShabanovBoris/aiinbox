from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.bot.keyboards import (
    item_keyboard,
    item_more_keyboard,
    motivation_reminder_keyboard,
    proactive_reminder_keyboard,
    reminder_more_keyboard,
    reminder_snooze_keyboard,
)
from app.domain.enums import ItemState, ItemType, MotivationKind, ProcessingStatus, SourceType
from app.services.actions import apply_item_action
from app.services.attention_ranking import AttentionRankingService
from app.services.reminder_feedback import (
    ReminderFeedbackService,
    record_reminder_event,
)
from app.storage.models import Delivery, Event, Item, ItemSource, Reminder, User

_NOW = datetime(2026, 9, 24, 12, 0)


async def _item_and_user(
    session_factory,
    *,
    telegram_user_id: int = 42,
    state: ItemState = ItemState.ACTIVE,
    category: str | None = "AI",
    item_type: ItemType | None = ItemType.READ,
    source_type: SourceType = SourceType.TEXT,
) -> tuple[int, int]:
    async with session_factory() as session:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id)
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            telegram_message_id=None,
            source_index=0,
            processing_status=ProcessingStatus.READY,
            state=state,
            source_type=source_type,
            processing_stage="READY",
            user_note="",
            title="Saved item",
            category=category,
            item_type=item_type,
            priority_score=80,
            interest_level=2,
            created_at=_NOW - timedelta(days=30),
        )
        session.add(item)
        await session.commit()
        return user.id, item.id


async def _reminder(
    session_factory,
    user_id: int,
    item_id: int | None,
    *,
    type_: str = "PROACTIVE_ATTENTION",
    status: str = "SENT",
    payload: dict | None = None,
    scheduled_at: datetime = _NOW - timedelta(hours=1),
) -> int:
    async with session_factory() as session:
        reminder = Reminder(
            user_id=user_id,
            item_id=item_id,
            type=type_,
            scheduled_at=scheduled_at,
            status=status,
            sent_at=_NOW - timedelta(minutes=30) if status == "SENT" else None,
            payload_json=payload
            or {
                "attention_score": 81,
                "priority_score": 80,
                "interest_level": 2,
                "policy_level": 4,
                "category": "AI",
                "item_type": "READ",
                "hook_content_id": 991,
                "template_id": "reason_to_return_v1",
            },
        )
        session.add(reminder)
        await session.commit()
        return reminder.id


async def _add_outcome(
    session_factory,
    user_id: int,
    item_id: int | None,
    *,
    reminder_type: str,
    event_type: str,
    created_at: datetime,
    category: str | None = "AI",
    item_type: str | None = "READ",
    motivation_kind: str | None = None,
    index: int = 0,
) -> int:
    payload = {"reminder_type": reminder_type}
    if category is not None:
        payload["category"] = category
    if item_type is not None:
        payload["item_type"] = item_type
    if motivation_kind is not None:
        payload["motivation_kind"] = motivation_kind
    async with session_factory() as session:
        reminder = Reminder(
            user_id=user_id,
            item_id=item_id,
            type=reminder_type,
            scheduled_at=_NOW - timedelta(days=300) + timedelta(microseconds=index),
            status="SENT",
            sent_at=created_at,
            payload_json=payload,
        )
        session.add(reminder)
        await session.flush()
        session.add(
            Event(
                user_id=user_id,
                item_id=item_id,
                reminder_id=reminder.id,
                event_type=event_type,
                payload_json=payload,
                created_at=created_at,
            )
        )
        await session.commit()
        return reminder.id


async def _event_count(session_factory, reminder_id: int, event_type: str) -> int:
    async with session_factory() as session:
        return await session.scalar(
            select(func.count(Event.id)).where(
                Event.reminder_id == reminder_id,
                Event.event_type == event_type,
            )
        )


async def test_reminder_done_is_atomic_scoped_and_semantically_idempotent(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    reminder_id = await _reminder(session_factory, user_id, item_id)
    service = ReminderFeedbackService(session_factory)

    assert await service.apply_callback(42, reminder_id, "done", callback_id="done-1") == "APPLIED"
    assert await service.apply_callback(42, reminder_id, "done", callback_id="done-2") == (
        "ALREADY_RECORDED"
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        events = list(
            (
                await session.scalars(
                    select(Event).where(Event.reminder_id == reminder_id).order_by(Event.id)
                )
            ).all()
        )
        assert item.state is ItemState.DONE
        assert item.priority_score == 80 and item.interest_level == 2
        assert [event.event_type for event in events] == ["REMINDER_DONE"]
        assert events[0].item_id == item_id
        assert events[0].payload_json == {
            "reminder_type": "PROACTIVE_ATTENTION",
            "attention_score": 81,
            "priority_score": 80,
            "interest_level": 2,
            "policy_level": 4,
            "category": "AI",
            "item_type": "READ",
            "hook_content_id": 991,
            "template_id": "reason_to_return_v1",
        }
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "DONE"
                )
            )
            == 1
        )


async def test_done_from_normal_item_ui_is_not_attributed_to_a_reminder(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    reminder_id = await _reminder(session_factory, user_id, item_id)
    await apply_item_action(session_factory, 42, item_id, "done")

    assert (
        await ReminderFeedbackService(session_factory).apply_callback(
            42, reminder_id, "done", callback_id="stale-done"
        )
        == "ALREADY_DONE"
    )
    assert await _event_count(session_factory, reminder_id, "REMINDER_DONE") == 0


async def test_reminder_snooze_commits_lifecycle_and_outcome_once(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    reminder_id = await _reminder(session_factory, user_id, item_id)
    service = ReminderFeedbackService(session_factory)
    first_until = _NOW + timedelta(days=1)

    assert (
        await service.apply_callback(
            42, reminder_id, "snooze", callback_id="later-1", snoozed_until=first_until
        )
        == "APPLIED"
    )
    assert (
        await service.apply_callback(
            42,
            reminder_id,
            "snooze",
            callback_id="later-2",
            snoozed_until=_NOW + timedelta(days=7),
        )
        == "ALREADY_RECORDED"
    )

    async with session_factory() as session:
        item = await session.get(Item, item_id)
        assert item.state is ItemState.SNOOZED
        assert item.snoozed_until == first_until
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "SNOOZED"
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.reminder_id == reminder_id,
                    Event.event_type == "REMINDER_SNOOZED",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.item_id == item_id, Reminder.type == "SNOOZE_RESURFACE"
                )
            )
            == 1
        )


async def test_dismiss_and_dislike_leave_canonical_item_fields_unchanged(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    dismiss_id = await _reminder(session_factory, user_id, item_id)
    dislike_id = await _reminder(
        session_factory,
        user_id,
        item_id,
        scheduled_at=_NOW - timedelta(minutes=20),
    )
    service = ReminderFeedbackService(session_factory)
    assert await service.apply_callback(42, dismiss_id, "dismiss", callback_id="dismiss-1") == (
        "APPLIED"
    )
    assert await service.apply_callback(42, dislike_id, "dislike", callback_id="dislike-1") == (
        "APPLIED"
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        assert item.state is ItemState.ACTIVE
        assert item.priority_score == 80 and item.interest_level == 2
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "NOT_INTERESTING"
                )
            )
            == 0
        )
    assert await _event_count(session_factory, dismiss_id, "REMINDER_DISMISSED") == 1
    assert await _event_count(session_factory, dislike_id, "REMINDER_DISLIKED") == 1


async def test_generic_nudge_dislike_has_no_item_and_ok_is_not_a_fake_event(session_factory):
    user_id, _item_id = await _item_and_user(session_factory)
    nudge_id = await _reminder(
        session_factory,
        user_id,
        None,
        type_="MOTIVATION_NUDGE",
        payload={
            "kind": "QUICK_WINS",
            "template_id": "quick_wins_v2",
            "policy_level": 5,
            "facts": {"count": 3},
        },
    )
    service = ReminderFeedbackService(session_factory)
    assert await service.apply_callback(42, nudge_id, "ok", callback_id="ok-1") == "APPLIED"
    assert await _event_count(session_factory, nudge_id, "REMINDER_OPENED") == 0
    assert await _event_count(session_factory, nudge_id, "REMINDER_DONE") == 0
    assert await service.apply_callback(42, nudge_id, "dislike", callback_id="less-1") == "APPLIED"
    async with session_factory() as session:
        event = await session.scalar(
            select(Event).where(
                Event.reminder_id == nudge_id,
                Event.event_type == "REMINDER_DISLIKED",
            )
        )
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert event.item_id is None
        assert event.payload_json == {
            "reminder_type": "MOTIVATION_NUDGE",
            "policy_level": 5,
            "template_id": "quick_wins_v2",
            "motivation_kind": "QUICK_WINS",
        }
        assert user.settings_json.get("generic_motivation_enabled", True) is True


async def test_wrong_user_and_non_sent_reminder_cannot_create_feedback(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    async with session_factory() as session:
        other = User(telegram_user_id=1000, telegram_chat_id=1000)
        session.add(other)
        await session.commit()
    sent_id = await _reminder(session_factory, user_id, item_id)
    failed_id = await _reminder(
        session_factory,
        user_id,
        item_id,
        status="FAILED",
        scheduled_at=_NOW - timedelta(minutes=5),
    )
    service = ReminderFeedbackService(session_factory)
    assert await service.apply_callback(1000, sent_id, "done", callback_id="foreign") == (
        "NOT_FOUND"
    )
    assert await service.apply_callback(42, failed_id, "dislike", callback_id="failed") == (
        "UNAVAILABLE"
    )
    assert await _event_count(session_factory, sent_id, "REMINDER_DONE") == 0
    assert await _event_count(session_factory, failed_id, "REMINDER_DISLIKED") == 0


async def test_observable_video_open_records_once_and_reuses_delivery(session_factory):
    user_id, item_id = await _item_and_user(session_factory, source_type=SourceType.YOUTUBE)
    async with session_factory() as session:
        source = ItemSource(
            item_id=item_id,
            source_index=0,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=abc",
            extraction_status="READY",
        )
        session.add(source)
        await session.commit()
        source_id = source.id
    reminder_id = await _reminder(session_factory, user_id, item_id)
    service = ReminderFeedbackService(session_factory)

    assert (
        await service.apply_callback(
            42, reminder_id, "open", callback_id="open-1", source_id=source_id
        )
        == "QUEUED"
    )
    assert (
        await service.apply_callback(
            42, reminder_id, "open", callback_id="open-2", source_id=source_id
        )
        == "IN_PROGRESS"
    )
    assert await _event_count(session_factory, reminder_id, "REMINDER_OPENED") == 1
    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"ITEM_VIDEO:{source_id}",
            )
        )
        assert delivery is not None and delivery.status == "PENDING"
        delivery.status = "SENT"
        delivery.sent_at = _NOW
        await session.commit()

    # A transport retry after terminal delivery must not reopen the outbox.
    assert (
        await service.apply_callback(
            42, reminder_id, "open", callback_id="open-2", source_id=source_id
        )
        == "DUPLICATE_CALLBACK"
    )
    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"ITEM_VIDEO:{source_id}",
            )
        )
        assert delivery.status == "SENT"

    # A deliberate new tap has a new callback identity and may enqueue a resend.
    assert (
        await service.apply_callback(
            42, reminder_id, "open", callback_id="open-3", source_id=source_id
        )
        == "QUEUED"
    )
    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"ITEM_VIDEO:{source_id}",
            )
        )
        assert delivery.status == "PENDING"
    assert await _event_count(session_factory, reminder_id, "REMINDER_OPENED") == 1


async def test_open_wrong_source_creates_neither_event_nor_delivery(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    _other_user_id, other_item_id = await _item_and_user(session_factory, telegram_user_id=1000)
    async with session_factory() as session:
        source = ItemSource(
            item_id=other_item_id,
            source_index=0,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=other",
            extraction_status="READY",
        )
        session.add(source)
        await session.commit()
        source_id = source.id
    reminder_id = await _reminder(session_factory, user_id, item_id)

    assert (
        await ReminderFeedbackService(session_factory).apply_callback(
            42, reminder_id, "open", callback_id="wrong-source", source_id=source_id
        )
        == "UNAVAILABLE"
    )
    assert await _event_count(session_factory, reminder_id, "REMINDER_OPENED") == 0
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Delivery.id))) == 0


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(days=30), -10),
        (timedelta(days=30, seconds=1), -5),
        (timedelta(days=90), -5),
        (timedelta(days=90, seconds=1), 0),
    ],
)
async def test_category_dislike_decay_boundaries(session_factory, age, expected):
    user_id, item_id = await _item_and_user(session_factory)
    await _add_outcome(
        session_factory,
        user_id,
        item_id,
        reminder_type="PROACTIVE_ATTENTION",
        event_type="REMINDER_DISLIKED",
        created_at=_NOW - age,
        category="AI",
        item_type="ACTION",
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        by_item, _fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert by_item[item_id] == expected


async def test_preference_penalty_is_non_stacking_clamped_and_ignores_generic_dislike(
    session_factory,
):
    user_id, item_id = await _item_and_user(session_factory)
    for index in range(5):
        await _add_outcome(
            session_factory,
            user_id,
            item_id,
            reminder_type="PROACTIVE_ATTENTION",
            event_type="REMINDER_DISLIKED",
            created_at=_NOW - timedelta(days=index),
            index=index,
        )
    await _add_outcome(
        session_factory,
        user_id,
        None,
        reminder_type="MOTIVATION_NUDGE",
        event_type="REMINDER_DISLIKED",
        created_at=_NOW,
        motivation_kind="QUICK_WINS",
        index=10,
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        by_item, _fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert by_item[item_id] == -12


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(days=30), -4),
        (timedelta(days=30, seconds=1), -2),
        (timedelta(days=90), -2),
        (timedelta(days=90, seconds=1), 0),
    ],
)
async def test_item_type_dislike_decay_boundaries(session_factory, age, expected):
    user_id, item_id = await _item_and_user(session_factory, category="Other")
    await _add_outcome(
        session_factory,
        user_id,
        item_id,
        reminder_type="PROACTIVE_ATTENTION",
        event_type="REMINDER_DISLIKED",
        created_at=_NOW - age,
        category="AI",
        item_type="READ",
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        by_item, _fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert by_item[item_id] == expected


@pytest.mark.parametrize(
    ("age", "expected"),
    [(timedelta(days=7), -2), (timedelta(days=7, seconds=1), 0)],
)
async def test_fatigue_window_boundary(session_factory, age, expected):
    user_id, item_id = await _item_and_user(session_factory)
    await _add_outcome(
        session_factory,
        user_id,
        item_id,
        reminder_type="PROACTIVE_ATTENTION",
        event_type="REMINDER_DISLIKED",
        created_at=_NOW - age,
    )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        _by_item, fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert fatigue == expected


async def test_fatigue_is_zero_without_history_and_positive_engagement_never_rewards(
    session_factory,
):
    user_id, item_id = await _item_and_user(session_factory)
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        _by_item, fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert fatigue == 0
    for index, event_type in enumerate(["REMINDER_DONE", "REMINDER_OPENED"] * 4):
        await _add_outcome(
            session_factory,
            user_id,
            item_id,
            reminder_type="PROACTIVE_ATTENTION",
            event_type=event_type,
            created_at=_NOW - timedelta(days=1),
            index=index,
        )
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        _by_item, fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert fatigue == 0


async def test_disliked_motivation_kind_expires_after_seven_elapsed_days(session_factory):
    user_id, _item_id = await _item_and_user(session_factory)
    await _add_outcome(
        session_factory,
        user_id,
        None,
        reminder_type="MOTIVATION_NUDGE",
        event_type="REMINDER_DISLIKED",
        created_at=_NOW - timedelta(days=6, hours=23, minutes=59),
        motivation_kind="QUICK_WINS",
        index=1,
    )
    async with session_factory() as session:
        assert await ReminderFeedbackService.suppressed_motivation_kinds(
            session, user_id, now=_NOW
        ) == {MotivationKind.QUICK_WINS}

    await _add_outcome(
        session_factory,
        user_id,
        None,
        reminder_type="MOTIVATION_NUDGE",
        event_type="REMINDER_DISLIKED",
        created_at=_NOW - timedelta(days=7),
        motivation_kind="STALE_IMPORTANT",
        index=2,
    )
    async with session_factory() as session:
        assert await ReminderFeedbackService.suppressed_motivation_kinds(
            session, user_id, now=_NOW
        ) == {MotivationKind.QUICK_WINS}


async def test_fatigue_is_smoothed_non_positive_bounded_and_user_scoped(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    _other_user_id, other_item_id = await _item_and_user(session_factory, telegram_user_id=1000)
    for index in range(30):
        await _add_outcome(
            session_factory,
            user_id,
            item_id,
            reminder_type="PROACTIVE_ATTENTION",
            event_type="REMINDER_DISLIKED",
            created_at=_NOW - timedelta(days=index % 7),
            index=index,
        )
    for index in range(10):
        await _add_outcome(
            session_factory,
            user_id,
            item_id,
            reminder_type="PROACTIVE_ATTENTION",
            event_type="REMINDER_DONE",
            created_at=_NOW - timedelta(days=1),
            index=100 + index,
        )
    async with session_factory() as session:
        other_item = await session.get(Item, other_item_id)
        _by_item, other_user_fatigue = await ReminderFeedbackService.attention_adjustments(
            session, other_item.user_id, [other_item], now=_NOW
        )
        assert other_user_fatigue == 0
        item = await session.get(Item, item_id)
        _by_item, fatigue = await ReminderFeedbackService.attention_adjustments(
            session, user_id, [item], now=_NOW
        )
        assert -10 <= fatigue <= 0


async def test_attention_rank_adds_feedback_without_mutating_priority_or_pm06(session_factory):
    user_id, item_id = await _item_and_user(session_factory)
    service = AttentionRankingService()
    async with session_factory() as session:
        item, before = await service.rank_item(session, user_id, item_id, now=_NOW)
        original_priority = item.priority_score
        original_personal_rank = before.personal_rank
    reminder_id = await _reminder(session_factory, user_id, item_id)
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        record_reminder_event(
            session,
            reminder,
            "REMINDER_DISLIKED",
            created_at=_NOW,
            callback_id="rank-dislike",
        )
        await session.commit()
    async with session_factory() as session:
        item, ranked = await service.rank_item(session, user_id, item_id, now=_NOW)
        listed_item, listed = (await service.list_candidates(session, user_id, now=_NOW))[0]
        assert ranked.reminder_preference_penalty == -12
        assert ranked.notification_fatigue_penalty == -2
        assert ranked.score == max(0, before.score - 14)
        assert ranked.personal_rank == original_personal_rank
        assert ranked == listed
        assert listed_item.id == item_id
        assert item.priority_score == original_priority


def _callback_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_reminder_keyboards_are_compact_identity_preserving_and_bounded(session_factory):
    item = Item(
        id=123,
        user_id=1,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.YOUTUBE,
        processing_stage="READY",
        user_note="",
    )
    source = ItemSource(
        id=321,
        item_id=123,
        source_index=0,
        source_type=SourceType.YOUTUBE,
        source_url="https://www.youtube.com/watch?v=abc",
        extraction_status="READY",
    )
    proactive = proactive_reminder_keyboard(456, item, [source], focus_source_id=321)
    motivation = motivation_reminder_keyboard(456)
    snooze = reminder_snooze_keyboard(456)
    callbacks = _callback_data(proactive) + _callback_data(motivation) + _callback_data(snooze)
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
    assert "reminder:open:456:321" in callbacks
    proactive_actions = {
        "reminder:later:456",
        "reminder:done:456",
        "reminder:dismiss:456",
        "reminder:less:456",
    }
    assert "reminder:more:456" in _callback_data(proactive)
    assert not proactive_actions & set(_callback_data(proactive))
    assert proactive_actions <= set(_callback_data(reminder_more_keyboard(456)))
    assert {"nav:attention:show:3", "reminder:less:456"} <= set(_callback_data(motivation))
    assert "reminder:snooze:456:tomorrow" in _callback_data(snooze)

    primary = _callback_data(item_keyboard(item, [source]))
    assert f"item:video:{item.id}:{source.id}" in primary
    assert f"item:more:{item.id}" in primary
    assert not any(
        value.startswith(("feedback:", "item:done:", "item:later:", "item:archive:"))
        for value in primary
    )
    secondary = _callback_data(item_more_keyboard(item))
    assert f"item:done:{item.id}" in secondary
    assert f"item:later:{item.id}" in secondary
    assert f"item:archive:{item.id}" in secondary
    assert f"feedback:menu:{item.id}" in secondary


def test_reminder_primary_keeps_hook_source_ahead_of_other_media_actions():
    item = Item(
        id=124,
        user_id=1,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.YOUTUBE,
        processing_stage="READY",
        user_note="",
    )
    videos = [
        ItemSource(
            id=source_id,
            item_id=item.id,
            source_index=source_id,
            source_type=SourceType.YOUTUBE,
            source_url=f"https://www.youtube.com/watch?v=video{source_id}",
            extraction_status="READY",
        )
        for source_id in (201, 202, 203)
    ]
    focus = ItemSource(
        id=777,
        item_id=item.id,
        source_index=4,
        source_type=SourceType.WEB,
        source_url="https://focus.example.com/result",
        extraction_status="READY",
    )

    markup = proactive_reminder_keyboard(456, item, [*videos, focus], focus_source_id=focus.id)
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert buttons[0].url == "https://focus.example.com/result"
    assert (
        sum(
            button.url is not None or button.callback_data.startswith("reminder:open:")
            for button in buttons
        )
        <= 2
    )
    assert "reminder:sources:456" in _callback_data(markup)
