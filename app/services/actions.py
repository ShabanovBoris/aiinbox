"""Transactional user actions for the Item lifecycle (Phase 10)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import ItemState, ProcessingStatus
from app.services.notifications import add_snooze_reminder, cancel_snooze_reminders
from app.storage.models import Event, Item, User


def _utc_now() -> datetime:
    """Store UTC consistently with the project's naive SQLite DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


async def apply_item_action(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    action: str,
    snoozed_until: datetime | None = None,
) -> Item | None:
    """Apply one scoped, idempotent transition and persist its event atomically.

    The service owns lifecycle mutation; Telegram callbacks only translate input
    and render the result, so retries cannot bypass user scoping or transactions.
    """
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            return None
        item = await session.scalar(select(Item).where(Item.id == item_id, Item.user_id == user.id))
        if item is None:
            return None

        event_type: str | None = None
        if action == "done":
            if item.state is not ItemState.DONE:
                item.state = ItemState.DONE
                item.completed_at = _utc_now()
                item.snoozed_until = None
                await cancel_snooze_reminders(session, user.id, item.id)
                event_type = "DONE"
        elif action == "archive":
            if item.state is not ItemState.ARCHIVED:
                item.state = ItemState.ARCHIVED
                item.archived_at = _utc_now()
                item.snoozed_until = None
                await cancel_snooze_reminders(session, user.id, item.id)
                event_type = "ARCHIVED"
        elif action == "snooze" and snoozed_until is not None:
            if item.state is not ItemState.SNOOZED or item.snoozed_until != snoozed_until:
                item.state = ItemState.SNOOZED
                item.snoozed_until = snoozed_until
                event_type = "SNOOZED"
                await cancel_snooze_reminders(session, user.id, item.id)
                await add_snooze_reminder(session, user.id, item.id, snoozed_until)
        elif action == "cancel_snooze":
            if item.state is ItemState.SNOOZED:
                item.state = ItemState.ACTIVE
                item.snoozed_until = None
                await cancel_snooze_reminders(session, user.id, item.id)
        elif action == "retry":
            if item.processing_status is ProcessingStatus.FAILED:
                item.processing_status = ProcessingStatus.QUEUED
                item.error_code = None
                item.error_message = None
                event_type = "RETRIED"
        else:
            return item

        if event_type is not None:
            session.add(
                Event(
                    user_id=user.id,
                    item_id=item.id,
                    event_type=event_type,
                    payload_json={"snoozed_until": snoozed_until.isoformat()}
                    if snoozed_until is not None
                    else None,
                )
            )
        await session.commit()
        return item


async def record_item_events(
    session: AsyncSession, user_id: int, item_ids: list[int], event_type: str
) -> None:
    """Record a retrieval observation in the caller's existing transaction."""
    session.add_all(
        [Event(user_id=user_id, item_id=item_id, event_type=event_type) for item_id in item_ids]
    )
