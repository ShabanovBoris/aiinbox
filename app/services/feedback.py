"""Application operations for explicit feedback and canonical corrections.

Telegram supplies intent and callback identity; this service owns user scoping,
SQLite serialization, and the boundary between auxiliary Events and Item state.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import text

from app.domain.enums import ItemType, ProcessingStatus
from app.storage.models import Event, FeedbackCallbackReceipt, Item, User

_FEEDBACK_EVENT_TYPES = {
    "USEFUL",
    "NOT_INTERESTING",
    "PRIORITY_HIGHER",
    "PRIORITY_LOWER",
    "SUMMARY_REPORTED_WRONG",
}
_PRIORITY_EVENT_TYPES = {"PRIORITY_HIGHER", "PRIORITY_LOWER"}
_MAX_IDEMPOTENCY_KEY_LENGTH = 160
_MAX_CATEGORY_LENGTH = 100


def _validate_idempotency_key(idempotency_key: str) -> None:
    """Keep transport identity within its durable schema contract."""
    if not idempotency_key or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError("idempotency key must contain 1..160 characters")


async def _ready_item(session: AsyncSession, telegram_user_id: int, item_id: int) -> Item | None:
    """Resolve the READY Item inside the caller's transaction and user boundary."""
    return await session.scalar(
        select(Item)
        .join(User, User.id == Item.user_id)
        .where(
            User.telegram_user_id == telegram_user_id,
            Item.id == item_id,
            Item.processing_status == ProcessingStatus.READY,
        )
    )


async def _has_callback_event(session: AsyncSession, user_id: int, idempotency_key: str) -> bool:
    """Check a namespaced callback key after BEGIN IMMEDIATE serializes writers."""
    return (
        await session.scalar(
            select(Event.id).where(
                Event.user_id == user_id,
                Event.idempotency_key == idempotency_key,
            )
        )
        is not None
    )


async def _has_callback_receipt(session: AsyncSession, user_id: int, idempotency_key: str) -> bool:
    """Detect a correction callback already consumed without creating an Event."""
    return (
        await session.scalar(
            select(FeedbackCallbackReceipt.id).where(
                FeedbackCallbackReceipt.user_id == user_id,
                FeedbackCallbackReceipt.idempotency_key == idempotency_key,
            )
        )
        is not None
    )


async def record_item_feedback(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    event_type: str,
    *,
    idempotency_key: str,
) -> Item | None:
    """Append one READY Item signal without changing any canonical Item fields.

    The durable key makes Telegram redelivery harmless while a later click has a
    new key and therefore remains a separate history entry.
    """
    if event_type not in _FEEDBACK_EVENT_TYPES:
        raise ValueError("unsupported feedback event")
    _validate_idempotency_key(idempotency_key)

    async with session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        item = await _ready_item(session, telegram_user_id, item_id)
        if item is None:
            await session.rollback()
            return None
        if await _has_callback_event(session, item.user_id, idempotency_key):
            await session.commit()
            return item

        payload = {"source": "telegram", "surface": "item_result"}
        if event_type in _PRIORITY_EVENT_TYPES:
            payload["priority_score_at_feedback"] = item.priority_score
        session.add(
            Event(
                user_id=item.user_id,
                item_id=item.id,
                event_type=event_type,
                payload_json=payload,
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return item


async def correct_item_category(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    new_category: str,
    *,
    idempotency_key: str,
) -> tuple[Item, bool] | None:
    """Atomically correct an existing user category and append its transition.

    The writer lock keeps the recorded previous value aligned with the category
    that actually preceded each committed correction.
    """
    category = new_category.strip()
    if not category or len(category) > _MAX_CATEGORY_LENGTH:
        raise ValueError("category must contain 1..100 characters")
    _validate_idempotency_key(idempotency_key)

    async with session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        item = await _ready_item(session, telegram_user_id, item_id)
        if item is None:
            await session.rollback()
            return None
        if await _has_callback_event(
            session, item.user_id, idempotency_key
        ) or await _has_callback_receipt(session, item.user_id, idempotency_key):
            await session.commit()
            return item, False
        # Persist the transport outcome even for a canonical no-op. Without
        # this separate receipt, a delayed replay could mutate a later value.
        session.add(FeedbackCallbackReceipt(user_id=item.user_id, idempotency_key=idempotency_key))
        if item.category == category:
            await session.commit()
            return item, False

        category_exists = await session.scalar(
            select(Item.id).where(Item.user_id == item.user_id, Item.category == category).limit(1)
        )
        if category_exists is None:
            await session.commit()
            return None

        previous = item.category
        item.category = category
        session.add(
            Event(
                user_id=item.user_id,
                item_id=item.id,
                event_type="CATEGORY_CORRECTED",
                payload_json={
                    "from": previous,
                    "to": category,
                    "source": "telegram",
                },
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return item, True


async def correct_item_type(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    new_type: ItemType | str,
    *,
    idempotency_key: str,
) -> tuple[Item, bool] | None:
    """Atomically correct ItemType without rerunning analysis or priority."""
    try:
        item_type = new_type if isinstance(new_type, ItemType) else ItemType(new_type)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported ItemType") from exc
    _validate_idempotency_key(idempotency_key)

    async with session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        item = await _ready_item(session, telegram_user_id, item_id)
        if item is None:
            await session.rollback()
            return None
        if await _has_callback_event(
            session, item.user_id, idempotency_key
        ) or await _has_callback_receipt(session, item.user_id, idempotency_key):
            await session.commit()
            return item, False
        # No-op corrections have no semantic Event, so their callback identity
        # needs its own durable row within the same serialized transaction.
        session.add(FeedbackCallbackReceipt(user_id=item.user_id, idempotency_key=idempotency_key))
        if item.item_type is item_type:
            await session.commit()
            return item, False

        previous = item.item_type.value if item.item_type is not None else None
        item.item_type = item_type
        session.add(
            Event(
                user_id=item.user_id,
                item_id=item.id,
                event_type="TYPE_CORRECTED",
                payload_json={
                    "from": previous,
                    "to": item_type.value,
                    "source": "telegram",
                },
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return item, True
