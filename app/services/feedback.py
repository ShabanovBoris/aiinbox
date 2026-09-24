"""Application operations for explicit feedback and canonical corrections.

Telegram supplies intent and callback identity; this service owns user scoping,
SQLite serialization, and the boundary between auxiliary Events and Item state.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import text

from app.domain.category_tokens import category_token
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
    """Detect a callback identity already consumed independently of Event history."""
    return (
        await session.scalar(
            select(FeedbackCallbackReceipt.id).where(
                FeedbackCallbackReceipt.user_id == user_id,
                FeedbackCallbackReceipt.idempotency_key == idempotency_key,
            )
        )
        is not None
    )


async def _callback_was_consumed(session: AsyncSession, user_id: int, idempotency_key: str) -> bool:
    """Check both semantic Event keys and receipts for no-op/unapplied outcomes."""
    return await _has_callback_event(
        session, user_id, idempotency_key
    ) or await _has_callback_receipt(session, user_id, idempotency_key)


async def claim_feedback_callback_receipt(
    session: AsyncSession, user_id: int, idempotency_key: str
) -> bool:
    """Claim transport identity inside BEGIN IMMEDIATE, including no-op outcomes.

    The receipt is separate from semantic Events so one durable outcome may
    safely support multiple distinct user actions while each callback retry is
    still consumed exactly once.
    """
    if await _callback_was_consumed(session, user_id, idempotency_key):
        return False
    session.add(FeedbackCallbackReceipt(user_id=user_id, idempotency_key=idempotency_key))
    return True


async def _claim_unapplied_feedback_callback(
    session: AsyncSession,
    telegram_user_id: int,
    item_id: int,
    idempotency_key: str,
) -> bool:
    """Claim an unavailable callback for its owner inside the active transaction.

    The caller holds BEGIN IMMEDIATE, so the eligibility decision and receipt
    commit cannot be separated by a retry that observes newer Item state.
    """
    owner_id = await session.scalar(
        select(Item.user_id)
        .join(User, User.id == Item.user_id)
        .where(User.telegram_user_id == telegram_user_id, Item.id == item_id)
    )
    if owner_id is None:
        return False
    await claim_feedback_callback_receipt(session, owner_id, idempotency_key)
    return True


def _apply_category_correction(
    session: AsyncSession, item: Item, category: str, idempotency_key: str
) -> bool:
    """Keep the canonical category change and its transition Event together."""
    if item.category == category:
        return False
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
    return True


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
            if await _claim_unapplied_feedback_callback(
                session, telegram_user_id, item_id, idempotency_key
            ):
                await session.commit()
            else:
                await session.rollback()
            return None
        if await _callback_was_consumed(session, item.user_id, idempotency_key):
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
            if await _claim_unapplied_feedback_callback(
                session, telegram_user_id, item_id, idempotency_key
            ):
                await session.commit()
            else:
                await session.rollback()
            return None
        if not await claim_feedback_callback_receipt(session, item.user_id, idempotency_key):
            await session.commit()
            return item, False
        # Persist the transport outcome even for a canonical no-op. Without
        # this separate receipt, a delayed replay could mutate a later value.
        if item.category == category:
            await session.commit()
            return item, False

        category_exists = await session.scalar(
            select(Item.id).where(Item.user_id == item.user_id, Item.category == category).limit(1)
        )
        if category_exists is None:
            await session.commit()
            return None

        changed = _apply_category_correction(session, item, category, idempotency_key)
        await session.commit()
        return item, changed


async def correct_item_category_by_token(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    selected_category_token: str,
    *,
    idempotency_key: str,
) -> tuple[Item, bool] | None:
    """Resolve a menu token and apply its category correction in one transaction.

    Resolving inside the writer transaction means a stale menu choice is
    consumed before that category can disappear and later become applicable.
    """
    _validate_idempotency_key(idempotency_key)

    async with session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        item = await _ready_item(session, telegram_user_id, item_id)
        if item is None:
            if await _claim_unapplied_feedback_callback(
                session, telegram_user_id, item_id, idempotency_key
            ):
                await session.commit()
            else:
                await session.rollback()
            return None
        if not await claim_feedback_callback_receipt(session, item.user_id, idempotency_key):
            await session.commit()
            return item, False

        categories = list(
            (
                await session.scalars(
                    select(Item.category)
                    .where(Item.user_id == item.user_id, Item.category.is_not(None))
                    .distinct()
                )
            ).all()
        )
        matches = [
            category
            for category in categories
            if category_token(category) == selected_category_token
        ]
        if len(matches) != 1:
            await session.commit()
            return None

        category = matches[0].strip()
        if not category or len(category) > _MAX_CATEGORY_LENGTH:
            await session.commit()
            return None
        changed = _apply_category_correction(session, item, category, idempotency_key)
        await session.commit()
        return item, changed


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
            if await _claim_unapplied_feedback_callback(
                session, telegram_user_id, item_id, idempotency_key
            ):
                await session.commit()
            else:
                await session.rollback()
            return None
        if not await claim_feedback_callback_receipt(session, item.user_id, idempotency_key):
            await session.commit()
            return item, False
        # No-op corrections have no semantic Event, so their callback identity
        # needs its own durable row within the same serialized transaction.
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
