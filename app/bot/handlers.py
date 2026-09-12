import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.domain.enums import SourceType
from app.errors import AppError
from app.services.ingestion import ingest_message, ingest_voice

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
    result = await ingest_message(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=message.text,
    )
    ack_lines = []
    if result.items:
        if len(result.items) == 1 and result.items[0].source_type is SourceType.TEXT:
            ack_lines.append("Принял. Разбираю…")
        else:
            ack_lines.append(f"Принял {len(result.items)} ссылок. Разбираю…")
    if result.duplicates:
        ack_lines.append("Часть ссылок уже сохранена — дубли пропустил.")
    if ack_lines:
        await message.answer("\n".join(ack_lines))


async def on_voice_audio(
    message: Message,
    settings: Settings,
    session_factory: async_sessionmaker,
    media,  # MessageVoice | MessageAudio
    is_audio: bool,
    max_audio_bytes: int,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        log.warning("unauthorized telegram user ignored user_id=%s", user_id)
        return
    file_size = media.file_size or 0
    if file_size > max_audio_bytes:
        # TOO_LARGE: отказ до постановки в очередь — файл всё равно не скачается.
        await message.answer("Файл слишком большой — лимит 20 МБ.")
        return
    source_type = SourceType.AUDIO if is_audio else SourceType.VOICE
    # persist → ACK (порядок Phase 1).
    try:
        await ingest_voice(
            session_factory,
            telegram_user_id=user_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            file_id=media.file_id,
            duration_seconds=media.duration,
            source_type=source_type,
        )
    except AppError as exc:
        log.warning("voice ingestion failed code=%s", exc.code)
        await message.answer(f"Не удалось сохранить файл ({exc.code}).")
        return
    label = "аудио" if is_audio else "голосовое"
    await message.answer(f"Принял {label}. Разбираю…")


def make_router(
    settings: Settings, session_factory: async_sessionmaker, max_audio_bytes: int = 20_000_000
) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await on_start(message, settings)

    # Не-командный текст — источники TEXT/WEB; медиа-источники добавляются
    # в своих фазах и идут через тот же pipeline.
    @router.message(F.text, ~F.text.startswith("/"))
    async def text(message: Message) -> None:
        await on_text(message, settings, session_factory)

    @router.message(F.voice)
    async def voice(message: Message) -> None:
        await on_voice_audio(
            message, settings, session_factory, message.voice, False, max_audio_bytes
        )

    @router.message(F.audio)
    async def audio(message: Message) -> None:
        await on_voice_audio(
            message, settings, session_factory, message.audio, True, max_audio_bytes
        )

    return router
