"""Transactional user actions for the Item lifecycle (Phase 10)."""

from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import text

from app.domain.enums import ItemState, ProcessingStatus
from app.services.delivery import ITEM_FAILED
from app.services.notifications import add_snooze_reminder, cancel_snooze_reminders
from app.storage.models import Delivery, Event, Item, User


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
        user_id = await session.scalar(
            select(User.id).where(User.telegram_user_id == telegram_user_id)
        )
        if user_id is None:
            return None

        # ❌ Удален ORM read-modify-write lifecycle block: concurrent callbacks
        # могли оба записать state и продублировать Event; transition теперь CAS.
        transition = None
        event_type: str | None = None
        if action == "done":
            transition = (
                update(Item)
                .where(
                    Item.id == item_id,
                    Item.user_id == user_id,
                    Item.state.in_((ItemState.ACTIVE, ItemState.SNOOZED)),
                )
                .values(
                    state=ItemState.DONE,
                    completed_at=_utc_now(),
                    snoozed_until=None,
                )
                .returning(Item.id)
            )
            event_type = "DONE"
        elif action == "archive":
            transition = (
                update(Item)
                .where(
                    Item.id == item_id,
                    Item.user_id == user_id,
                    Item.state.in_((ItemState.ACTIVE, ItemState.SNOOZED)),
                )
                .values(
                    state=ItemState.ARCHIVED,
                    archived_at=_utc_now(),
                    snoozed_until=None,
                )
                .returning(Item.id)
            )
            event_type = "ARCHIVED"
        elif action == "snooze" and snoozed_until is not None:
            transition = (
                update(Item)
                .where(
                    Item.id == item_id,
                    Item.user_id == user_id,
                    Item.state.in_((ItemState.ACTIVE, ItemState.SNOOZED)),
                    or_(
                        Item.state != ItemState.SNOOZED,
                        Item.snoozed_until.is_distinct_from(snoozed_until),
                    ),
                )
                .values(state=ItemState.SNOOZED, snoozed_until=snoozed_until)
                .returning(Item.id)
            )
            event_type = "SNOOZED"
        elif action == "cancel_snooze":
            transition = (
                update(Item)
                .where(
                    Item.id == item_id,
                    Item.user_id == user_id,
                    Item.state == ItemState.SNOOZED,
                )
                .values(state=ItemState.ACTIVE, snoozed_until=None)
                .returning(Item.id)
            )
        elif action == "retry":
            transition = (
                update(Item)
                .where(
                    Item.id == item_id,
                    Item.user_id == user_id,
                    Item.processing_status == ProcessingStatus.FAILED,
                )
                .values(
                    processing_status=ProcessingStatus.QUEUED,
                    error_code=None,
                    error_message=None,
                )
                .returning(Item.id)
            )
            event_type = "RETRIED"
        else:
            return await session.scalar(
                select(Item).where(Item.id == item_id, Item.user_id == user_id)
            )

        # The conditional UPDATE is the compare-and-set boundary. Only the
        # request that actually wins the transition may create side effects.
        transitioned_item_id = await session.scalar(transition)
        if transitioned_item_id is not None:
            if action in {"done", "archive", "cancel_snooze", "snooze"}:
                await cancel_snooze_reminders(session, user_id, item_id)
            if action == "snooze":
                await add_snooze_reminder(session, user_id, item_id, snoozed_until)
            if action == "retry":
                # Failure delivery belongs to the FAILED state being left. Marking
                # it terminal in the same transaction prevents the outbox from
                # announcing an obsolete failure after the user has already retried.
                await session.execute(
                    update(Delivery)
                    .where(
                        Delivery.item_id == item_id,
                        Delivery.type == ITEM_FAILED,
                        Delivery.status.in_(("PENDING", "SENDING")),
                    )
                    .values(status="CANCELLED", last_error=None)
                )

        if transitioned_item_id is not None and event_type is not None:
            session.add(
                Event(
                    user_id=user_id,
                    item_id=item_id,
                    event_type=event_type,
                    payload_json={"snoozed_until": snoozed_until.isoformat()}
                    if snoozed_until is not None
                    else None,
                )
            )
        await session.commit()
        return await session.scalar(select(Item).where(Item.id == item_id, Item.user_id == user_id))


async def set_item_interest(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    level: int,
) -> Item | None:
    """Own the canonical interest mutation and its durable feedback event.

    This application boundary keeps Telegram free of persistence logic and
    deliberately leaves PriorityEngine/model-derived ``interest_fit`` untouched.
    """
    if level not in {1, 2, 3}:
        raise ValueError("interest level must be between 1 and 3")

    async with session_factory() as session:
        # SQLite is the project's durable source of truth. Reserving its writer
        # lock before reading the old level serializes rapid callbacks so Event
        # payloads describe the real committed transition, not a stale snapshot.
        await session.execute(text("BEGIN IMMEDIATE"))
        item = await session.scalar(
            select(Item)
            .join(User, User.id == Item.user_id)
            .where(Item.id == item_id, User.telegram_user_id == telegram_user_id)
        )
        if item is None:
            await session.rollback()
            return None
        if item.interest_level == level:
            await session.commit()
            return item

        previous = item.interest_level
        changed_item_id = await session.scalar(
            update(Item)
            .where(
                Item.id == item.id,
                Item.user_id == item.user_id,
                Item.interest_level == previous,
            )
            .values(interest_level=level)
            .returning(Item.id)
        )
        if changed_item_id is None:
            await session.rollback()
            return None
        session.add(
            Event(
                user_id=item.user_id,
                item_id=item.id,
                event_type="INTEREST_CHANGED",
                payload_json={"from": previous, "to": level, "source": "telegram"},
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
