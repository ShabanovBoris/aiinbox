import asyncio

import pytest
from sqlalchemy import select

from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services import feedback as feedback_service
from app.services.feedback import (
    correct_item_category,
    correct_item_type,
    record_item_feedback,
)
from app.services.retrieval import TodayService, list_category_items
from app.storage.models import Event, FeedbackCallbackReceipt, Item, User


async def _create_item(
    session_factory,
    *,
    telegram_user_id=42,
    title="feedback target",
    category="Programming",
    item_type=ItemType.REFERENCE,
    state=ItemState.ACTIVE,
    status=ProcessingStatus.READY,
    interest_level=2,
    priority_score=72,
):
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            user = User(telegram_user_id=telegram_user_id)
            session.add(user)
            await session.flush()
        item = Item(
            user_id=user.id,
            telegram_message_id=None,
            source_index=0,
            processing_status=status,
            state=state,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            analysis_completeness="PARTIAL",
            user_note="keep the original note",
            title=title,
            summary="Original summary",
            category=category,
            item_type=item_type,
            priority_score=priority_score,
            interest_level=interest_level,
        )
        session.add(item)
        await session.commit()
        return item.id


async def _event_rows(session_factory, item_id: int) -> list[Event]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(Event).where(Event.item_id == item_id).order_by(Event.id)
                )
            ).all()
        )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("USEFUL", {"source": "telegram", "surface": "item_result"}),
        ("NOT_INTERESTING", {"source": "telegram", "surface": "item_result"}),
        (
            "PRIORITY_HIGHER",
            {
                "source": "telegram",
                "surface": "item_result",
                "priority_score_at_feedback": 72,
            },
        ),
        (
            "PRIORITY_LOWER",
            {
                "source": "telegram",
                "surface": "item_result",
                "priority_score_at_feedback": 72,
            },
        ),
        ("SUMMARY_REPORTED_WRONG", {"source": "telegram", "surface": "item_result"}),
    ],
)
async def test_event_only_feedback_preserves_canonical_item(session_factory, event_type, payload):
    item_id = await _create_item(session_factory, interest_level=3)

    item = await record_item_feedback(
        session_factory,
        42,
        item_id,
        event_type,
        idempotency_key=f"telegram-callback:{event_type}",
    )

    assert item is not None
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert event.user_id == stored.user_id
        assert event.item_id == item_id
        assert event.event_type == event_type
        assert event.payload_json == payload
        assert stored.state is ItemState.ACTIVE
        assert stored.priority_score == 72
        assert stored.interest_level == 3
        assert stored.category == "Programming"
        assert stored.item_type is ItemType.REFERENCE
        assert stored.summary == "Original summary"
        assert stored.processing_status is ProcessingStatus.READY
        assert stored.processing_stage == "READY"
        assert stored.analysis_completeness == "PARTIAL"


async def test_callback_idempotency_is_concurrent_and_later_feedback_is_allowed(
    session_factory,
):
    item_id = await _create_item(session_factory)

    await asyncio.gather(
        *[
            record_item_feedback(
                session_factory,
                42,
                item_id,
                "USEFUL",
                idempotency_key="telegram-callback:same",
            )
            for _ in range(4)
        ]
    )
    await record_item_feedback(
        session_factory,
        42,
        item_id,
        "USEFUL",
        idempotency_key="telegram-callback:later",
    )

    events = await _event_rows(session_factory, item_id)
    assert len(events) == 2
    assert [event.event_type for event in events] == ["USEFUL", "USEFUL"]
    assert len({event.idempotency_key for event in events}) == 2


async def test_feedback_is_scoped_to_user_and_ready_items(session_factory):
    item_id = await _create_item(session_factory)
    queued_id = await _create_item(
        session_factory, telegram_user_id=42, status=ProcessingStatus.QUEUED
    )

    assert (
        await record_item_feedback(
            session_factory, 1000, item_id, "USEFUL", idempotency_key="telegram-callback:other"
        )
        is None
    )
    assert (
        await record_item_feedback(
            session_factory, 42, queued_id, "USEFUL", idempotency_key="telegram-callback:queued"
        )
        is None
    )
    assert await _event_rows(session_factory, item_id) == []
    assert await _event_rows(session_factory, queued_id) == []


async def test_unavailable_feedback_receipt_is_atomic_with_ready_check(
    session_factory, monkeypatch
):
    await _create_item(session_factory)
    queued_id = await _create_item(session_factory, status=ProcessingStatus.QUEUED)
    await _create_item(session_factory, telegram_user_id=1000)
    async with session_factory() as session:
        queued_item = await session.get(Item, queued_id)
        user_id = queued_item.user_id
        assert queued_id != user_id
        other_user = await session.scalar(select(User).where(User.telegram_user_id == 1000))
        # This catches confusing Item.id with its owner when persisting receipts.
        assert other_user.id == queued_id

    callback_key = "telegram-callback:temporarily-unavailable"
    receipt_claimed = asyncio.Event()
    release_callback = asyncio.Event()
    original_claim = feedback_service._claim_feedback_callback_receipt
    paused = False

    async def pause_after_claim(session, user_id, idempotency_key):
        nonlocal paused
        claimed = await original_claim(session, user_id, idempotency_key)
        if idempotency_key == callback_key and claimed and not paused:
            paused = True
            receipt_claimed.set()
            await release_callback.wait()
        return claimed

    monkeypatch.setattr(feedback_service, "_claim_feedback_callback_receipt", pause_after_claim)
    first_delivery = asyncio.create_task(
        record_item_feedback(session_factory, 42, queued_id, "USEFUL", idempotency_key=callback_key)
    )
    await asyncio.wait_for(receipt_claimed.wait(), timeout=5)

    async def mark_ready():
        async with session_factory() as session:
            queued_item = await session.get(Item, queued_id)
            queued_item.processing_status = ProcessingStatus.READY
            await session.commit()

    ready_transition = asyncio.create_task(mark_ready())
    replay = asyncio.create_task(
        record_item_feedback(session_factory, 42, queued_id, "USEFUL", idempotency_key=callback_key)
    )
    foreign = asyncio.create_task(
        record_item_feedback(
            session_factory,
            1000,
            queued_id,
            "USEFUL",
            idempotency_key="telegram-callback:foreign",
        )
    )
    await asyncio.sleep(0)
    release_callback.set()
    first_result, _ready, replay_result, foreign_result = await asyncio.gather(
        first_delivery, ready_transition, replay, foreign
    )

    assert first_result is None
    assert foreign_result is None
    assert replay_result is None or replay_result.processing_status is ProcessingStatus.READY
    assert await _event_rows(session_factory, queued_id) == []
    async with session_factory() as session:
        queued_item = await session.get(Item, queued_id)
        assert queued_item.processing_status is ProcessingStatus.READY
        receipt_user_ids = list(
            (
                await session.scalars(
                    select(FeedbackCallbackReceipt.user_id).where(
                        FeedbackCallbackReceipt.idempotency_key.in_(
                            [callback_key, "telegram-callback:foreign"]
                        )
                    )
                )
            ).all()
        )
        assert receipt_user_ids == [user_id]


async def test_feedback_accepts_ready_done_item_without_changing_lifecycle(session_factory):
    item_id = await _create_item(session_factory, state=ItemState.DONE, interest_level=1)

    item = await record_item_feedback(
        session_factory, 42, item_id, "USEFUL", idempotency_key="telegram-callback:done"
    )

    assert item is not None and item.state is ItemState.DONE
    assert len(await _event_rows(session_factory, item_id)) == 1


async def test_useful_is_independent_from_low_interest_level(session_factory):
    item_id = await _create_item(session_factory, interest_level=1)

    item = await record_item_feedback(
        session_factory, 42, item_id, "USEFUL", idempotency_key="telegram-callback:useful"
    )

    assert item is not None and item.interest_level == 1
    assert (await _event_rows(session_factory, item_id))[0].event_type == "USEFUL"


async def test_category_correction_is_atomic_and_retrieval_uses_canonical_value(
    session_factory,
):
    item_id = await _create_item(session_factory)
    ai_item_id = await _create_item(session_factory, title="existing AI category", category="AI")

    result = await correct_item_category(
        session_factory,
        42,
        item_id,
        " AI ",
        idempotency_key="telegram-callback:category",
    )

    assert result is not None and result[1] is True
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert stored.category == "AI"
        assert stored.priority_score == 72
        assert stored.interest_level == 2
        assert stored.item_type is ItemType.REFERENCE
        assert stored.processing_stage == "READY"
        assert stored.analysis_completeness == "PARTIAL"
        assert event.event_type == "CATEGORY_CORRECTED"
        assert event.payload_json == {
            "from": "Programming",
            "to": "AI",
            "source": "telegram",
        }
        assert await list_category_items(session, stored.user_id, "Programming") == []
        assert {item.id for item in await list_category_items(session, stored.user_id, "AI")} == {
            item_id,
            ai_item_id,
        }


async def test_category_noop_invalid_and_nonexistent_targets_do_not_create_events(
    session_factory,
):
    item_id = await _create_item(session_factory)

    noop = await correct_item_category(
        session_factory,
        42,
        item_id,
        " Programming ",
        idempotency_key="telegram-callback:noop",
    )
    missing_category = await correct_item_category(
        session_factory,
        42,
        item_id,
        "New category",
        idempotency_key="telegram-callback:missing",
    )
    assert noop is not None and noop[1] is False
    assert missing_category is None
    with pytest.raises(ValueError):
        await correct_item_category(
            session_factory,
            42,
            item_id,
            "  ",
            idempotency_key="telegram-callback:empty",
        )
    with pytest.raises(ValueError):
        await correct_item_category(
            session_factory,
            42,
            item_id,
            "x" * 101,
            idempotency_key="telegram-callback:long",
        )
    assert await _event_rows(session_factory, item_id) == []


async def test_concurrent_category_corrections_record_real_transitions(session_factory):
    item_id = await _create_item(session_factory, category="Start")
    await _create_item(session_factory, title="Programming category", category="Programming")
    await _create_item(session_factory, title="Piano category", category="Piano")

    await asyncio.gather(
        correct_item_category(
            session_factory, 42, item_id, "Programming", idempotency_key="telegram-callback:a"
        ),
        correct_item_category(
            session_factory, 42, item_id, "Piano", idempotency_key="telegram-callback:b"
        ),
    )

    events = await _event_rows(session_factory, item_id)
    assert len(events) == 2
    assert events[0].payload_json["from"] == "Start"
    assert events[1].payload_json["from"] == events[0].payload_json["to"]
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.category == events[-1].payload_json["to"]


async def test_replayed_category_callback_cannot_reapply_after_a_later_correction(
    session_factory,
):
    item_id = await _create_item(session_factory, category="Start")
    await _create_item(session_factory, title="AI category", category="AI")
    await _create_item(session_factory, title="Piano category", category="Piano")

    first = await correct_item_category(
        session_factory, 42, item_id, "AI", idempotency_key="telegram-callback:first"
    )
    await correct_item_category(
        session_factory, 42, item_id, "Piano", idempotency_key="telegram-callback:second"
    )
    replay = await correct_item_category(
        session_factory, 42, item_id, "AI", idempotency_key="telegram-callback:first"
    )

    assert first is not None and first[1] is True
    assert replay is not None and replay[1] is False
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        events = list(
            (
                await session.scalars(
                    select(Event)
                    .where(Event.item_id == item_id, Event.event_type == "CATEGORY_CORRECTED")
                    .order_by(Event.id)
                )
            ).all()
        )
        assert stored.category == "Piano"
        assert len(events) == 2


async def test_noop_category_callback_receipt_prevents_late_replay(session_factory):
    item_id = await _create_item(session_factory, category="AI")
    await _create_item(session_factory, title="Programming category", category="Programming")

    noop = await correct_item_category(
        session_factory, 42, item_id, "AI", idempotency_key="telegram-callback:category-noop"
    )
    changed = await correct_item_category(
        session_factory,
        42,
        item_id,
        "Programming",
        idempotency_key="telegram-callback:category-change",
    )
    replay = await correct_item_category(
        session_factory, 42, item_id, "AI", idempotency_key="telegram-callback:category-noop"
    )

    assert noop is not None and noop[1] is False
    assert changed is not None and changed[1] is True
    assert replay is not None and replay[1] is False
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        corrections = list(
            (
                await session.scalars(
                    select(Event)
                    .where(Event.item_id == item_id, Event.event_type == "CATEGORY_CORRECTED")
                    .order_by(Event.id)
                )
            ).all()
        )
        receipts = list(
            (
                await session.scalars(
                    select(FeedbackCallbackReceipt.idempotency_key)
                    .where(FeedbackCallbackReceipt.user_id == stored.user_id)
                    .order_by(FeedbackCallbackReceipt.id)
                )
            ).all()
        )
        assert stored.category == "Programming"
        assert len(corrections) == 1
        assert corrections[0].payload_json == {
            "from": "AI",
            "to": "Programming",
            "source": "telegram",
        }
        assert receipts == ["telegram-callback:category-noop", "telegram-callback:category-change"]


async def test_type_correction_updates_today_and_keeps_semantic_priority(session_factory):
    item_id = await _create_item(session_factory, item_type=ItemType.REFERENCE)

    async with session_factory() as session:
        user_id = (await session.get(Item, item_id)).user_id
        assert await TodayService().list_items(session, user_id) == []

    result = await correct_item_type(
        session_factory,
        42,
        item_id,
        ItemType.LEARN,
        idempotency_key="telegram-callback:type",
    )

    assert result is not None and result[1] is True
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        today_items = await TodayService().list_items(session, stored.user_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert [item.id for item in today_items] == [item_id]
        assert stored.item_type is ItemType.LEARN
        assert stored.priority_score == 72
        assert event.event_type == "TYPE_CORRECTED"
        assert event.payload_json == {
            "from": "REFERENCE",
            "to": "LEARN",
            "source": "telegram",
        }


async def test_type_noop_invalid_and_unauthorized_corrections_are_safe(session_factory):
    item_id = await _create_item(session_factory, item_type=ItemType.LEARN)

    noop = await correct_item_type(
        session_factory,
        42,
        item_id,
        ItemType.LEARN,
        idempotency_key="telegram-callback:type-noop",
    )
    with pytest.raises(ValueError):
        await correct_item_type(
            session_factory,
            42,
            item_id,
            "NOT_A_TYPE",
            idempotency_key="telegram-callback:invalid",
        )
    denied = await correct_item_type(
        session_factory,
        1000,
        item_id,
        ItemType.READ,
        idempotency_key="telegram-callback:unauthorized",
    )

    assert noop is not None and noop[1] is False
    assert denied is None
    assert await _event_rows(session_factory, item_id) == []


async def test_noop_type_callback_receipt_prevents_late_replay(session_factory):
    item_id = await _create_item(session_factory, item_type=ItemType.LEARN)

    noops = await asyncio.gather(
        *[
            correct_item_type(
                session_factory,
                42,
                item_id,
                ItemType.LEARN,
                idempotency_key="telegram-callback:type-noop",
            )
            for _ in range(4)
        ]
    )
    changed = await correct_item_type(
        session_factory,
        42,
        item_id,
        ItemType.READ,
        idempotency_key="telegram-callback:type-change",
    )
    replay = await correct_item_type(
        session_factory,
        42,
        item_id,
        ItemType.LEARN,
        idempotency_key="telegram-callback:type-noop",
    )

    assert all(result is not None and result[1] is False for result in noops)
    assert changed is not None and changed[1] is True
    assert replay is not None and replay[1] is False
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        corrections = list(
            (
                await session.scalars(
                    select(Event)
                    .where(Event.item_id == item_id, Event.event_type == "TYPE_CORRECTED")
                    .order_by(Event.id)
                )
            ).all()
        )
        receipts = list(
            (
                await session.scalars(
                    select(FeedbackCallbackReceipt.idempotency_key)
                    .where(FeedbackCallbackReceipt.user_id == stored.user_id)
                    .order_by(FeedbackCallbackReceipt.id)
                )
            ).all()
        )
        assert stored.item_type is ItemType.READ
        assert len(corrections) == 1
        assert corrections[0].payload_json == {
            "from": "LEARN",
            "to": "READ",
            "source": "telegram",
        }
        assert receipts == ["telegram-callback:type-noop", "telegram-callback:type-change"]
