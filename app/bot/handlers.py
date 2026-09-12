import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.services.ingestion import ingest_text

log = logging.getLogger(__name__)


async def on_start(message: Message, settings: Settings) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer("Personal AI Inbox готов. Просто отправь текст или ссылку.")


async def on_text(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        # Allowlist: неавторизованным молча не отвечаем и ничего не обрабатываем.
        log.warning("unauthorized telegram user ignored user_id=%s", user_id)
        return
    if not message.text:
        return
    # Тяжёлая обработка запрещена в handler: только валидация, Item QUEUED и ответ.
    # Сначала persistence, потом ACK: при ошибке БД пользователь не получает
    # ложное подтверждение сохранения.
    await ingest_text(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=message.text,
    )
    await message.answer("Принял. Разбираю…")


def make_router(settings: Settings, session_factory: async_sessionmaker) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await on_start(message, settings)

    # Не-командный текст — единственный источник Phase 1; остальные источники
    # добавляются в следующих фазах (URL — Phase 3, voice — Phase 5 и т.д.).
    @router.message(F.text, ~F.text.startswith("/"))
    async def text(message: Message) -> None:
        await on_text(message, settings, session_factory)

    return router
