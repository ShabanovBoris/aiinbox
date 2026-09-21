import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.actions import apply_item_action
from app.storage.models import Event, Item, Reminder, User


async def make_failed_item(session_factory) -> tuple[int, int]:
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        item = Item(
            user_id=0,
            telegram_message_id=None,
            source_index=0,
            processing_status=ProcessingStatus.FAILED,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="ANALYZING",
            user_note="retry me",
            item_type=ItemType.ACTION,
            error_code="LLM_FAILED",
            error_message="temporary",
        )
        session.add(user)
        await session.flush()
        item.user_id = user.id
        session.add(item)
        await session.commit()
        return user.id, item.id


async def test_done_is_idempotent_and_persists_event(session_factory):
    _, item_id = await make_failed_item(session_factory)
    # FAILED is irrelevant to Done; the lifecycle transition is independent.
    first = await apply_item_action(session_factory, 42, item_id, "done")
    second = await apply_item_action(session_factory, 42, item_id, "done")
    assert first is not None and first.state is ItemState.DONE
    assert second is not None and second.state is ItemState.DONE
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "DONE"
                )
            )
            == 1
        )
        persisted = await session.get(Item, item_id)
        assert persisted.completed_at is not None


async def test_archive_and_snooze_survive_new_session(session_factory):
    _, item_id = await make_failed_item(session_factory)
    until = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    await apply_item_action(session_factory, 42, item_id, "snooze", until)
    async with session_factory() as session:
        persisted = await session.get(Item, item_id)
        assert persisted.state is ItemState.SNOOZED
        assert persisted.snoozed_until == until
    archived = await apply_item_action(session_factory, 42, item_id, "archive")
    assert archived is not None and archived.state is ItemState.ARCHIVED
    assert archived.archived_at is not None


async def test_retry_clears_error_and_reuses_checkpoint(session_factory):
    _, item_id = await make_failed_item(session_factory)
    item = await apply_item_action(session_factory, 42, item_id, "retry")
    assert item is not None
    assert item.processing_status is ProcessingStatus.QUEUED
    assert item.processing_stage == "ANALYZING"
    assert item.error_code is None and item.error_message is None
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "RETRIED"
                )
            )
            == 1
        )


async def test_retry_is_noop_for_non_failed_item(session_factory):
    _, item_id = await make_failed_item(session_factory)
    await apply_item_action(session_factory, 42, item_id, "retry")
    await apply_item_action(session_factory, 42, item_id, "retry")
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "RETRIED"
                )
            )
            == 1
        )


async def test_action_is_scoped_to_telegram_user(session_factory):
    _, item_id = await make_failed_item(session_factory)
    assert await apply_item_action(session_factory, 1000, item_id, "done") is None


async def test_concurrent_done_creates_one_transition_event(session_factory):
    _, item_id = await make_failed_item(session_factory)

    await asyncio.gather(
        apply_item_action(session_factory, 42, item_id, "done"),
        apply_item_action(session_factory, 42, item_id, "done"),
    )

    async with session_factory() as session:
        item = await session.get(Item, item_id)
        assert item.state is ItemState.DONE
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "DONE"
                )
            )
            == 1
        )


async def test_concurrent_terminal_actions_have_one_winner(session_factory):
    _, item_id = await make_failed_item(session_factory)

    await asyncio.gather(
        apply_item_action(session_factory, 42, item_id, "done"),
        apply_item_action(session_factory, 42, item_id, "archive"),
    )

    async with session_factory() as session:
        item = await session.get(Item, item_id)
        events = (
            await session.scalars(
                select(Event).where(
                    Event.item_id == item_id,
                    Event.event_type.in_(("DONE", "ARCHIVED")),
                )
            )
        ).all()
        assert len(events) == 1
        if item.state is ItemState.DONE:
            assert item.completed_at is not None
            assert item.archived_at is None
        else:
            assert item.state is ItemState.ARCHIVED
            assert item.archived_at is not None
            assert item.completed_at is None


async def test_concurrent_same_snooze_creates_one_event_and_reminder(session_factory):
    _, item_id = await make_failed_item(session_factory)
    until = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)

    await asyncio.gather(
        apply_item_action(session_factory, 42, item_id, "snooze", until),
        apply_item_action(session_factory, 42, item_id, "snooze", until),
    )

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "SNOOZED"
                )
            )
            == 1
        )
        assert (
            await session.scalar(select(func.count(Reminder.id)).where(Reminder.item_id == item_id))
            == 1
        )


async def test_concurrent_retry_creates_one_event(session_factory):
    _, item_id = await make_failed_item(session_factory)

    await asyncio.gather(
        apply_item_action(session_factory, 42, item_id, "retry"),
        apply_item_action(session_factory, 42, item_id, "retry"),
    )

    async with session_factory() as session:
        item = await session.get(Item, item_id)
        assert item.processing_status is ProcessingStatus.QUEUED
        assert (
            await session.scalar(
                select(func.count(Event.id)).where(
                    Event.item_id == item_id, Event.event_type == "RETRIED"
                )
            )
            == 1
        )
