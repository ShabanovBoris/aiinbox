import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import ProcessingStatus, SourceType
from app.storage.models import Item, User

log = logging.getLogger(__name__)


async def get_or_create_user(
    session: AsyncSession, *, telegram_user_id: int, chat_id: int | None
) -> User:
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=chat_id)
        session.add(user)
        await session.flush()
    return user


async def ingest_text(
    session_factory: async_sessionmaker,
    *,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
    text: str,
    source_index: int = 0,
) -> Item:
    """Идемпотентная точка входа текста: Telegram update → Item QUEUED.

    Уникальность (user_id, telegram_message_id, source_index) защищает от повторной
    обработки того же update на уровне БД; конфликт возвращает существующий Item.
    """
    async with session_factory() as session:
        user = await get_or_create_user(session, telegram_user_id=telegram_user_id, chat_id=chat_id)
        # user.id фиксируется до транзакции: rollback истекает объекты, и чтение
        # атрибута при построении запроса стало бы синхронным IO вне greenlet.
        user_id = user.id
        item = Item(
            user_id=user_id,
            telegram_message_id=message_id,
            source_index=source_index,
            processing_status=ProcessingStatus.QUEUED,
            source_type=SourceType.TEXT,
            user_note=text,
        )
        session.add(item)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(Item).where(
                    Item.user_id == user_id,
                    Item.telegram_message_id == message_id,
                    Item.source_index == source_index,
                )
            )
            if existing is None:
                raise
            log.info("duplicate telegram update ignored item_id=%s", existing.id)
            return existing
        log.info(
            "item queued id=%s user_id=%s source_type=%s", item.id, user_id, item.source_type.value
        )
        return item
