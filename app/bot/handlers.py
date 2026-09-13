import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.domain.enums import SourceType
from app.services.ingestion import ingest_message, ingest_voice
from app.services.profile import enqueue_profile_update

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
    source_type = SourceType.AUDIO if is_audio else SourceType.VOICE
    # persist → ACK (порядок Phase 1). Oversized сохраняется атомарно при создании
    # как FAILED/TOO_LARGE (PRODUCT_SPEC §66) — без claimable промежуточного
    # состояния, чтобы воркер не начал скачивание.
    oversized = (file_size, max_audio_bytes) if file_size > max_audio_bytes else None
    result = await ingest_voice(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=media.file_id,
        duration_seconds=media.duration,
        source_type=source_type,
        too_large=oversized,
    )
    item = result.items[0]
    if item.error_code == "TOO_LARGE":
        limit_mb = max_audio_bytes / 1_000_000
        await message.answer(
            f"Файл слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{limit_mb:.0f} МБ). Метаданные сохранил — файл не скачан."
        )
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

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        if not settings.is_allowed(message.from_user.id if message.from_user else None):
            return
        await on_profile(message, settings, session_factory)

    @router.message(Command("profile_update"))
    async def profile_update(message: Message) -> None:
        if not settings.is_allowed(message.from_user.id if message.from_user else None):
            return
        instruction = (message.text or "").removeprefix("/profile_update").strip()
        await on_profile_update(message, settings, session_factory, instruction)

    return router


async def on_profile(message: Message, settings: Settings, session_factory) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    from app.bot.formatting import format_profile
    from app.domain.models import UserProfile
    from app.storage.models import User as UserRow

    async with session_factory() as session:
        user = await session.scalar(
            select(UserRow).where(UserRow.telegram_user_id == message.from_user.id)
        )
        profile = UserProfile()
        if user is not None and user.profile_json:
            profile = UserProfile.model_validate(user.profile_json)
    await message.answer(format_profile(profile))


async def on_profile_update(
    message: Message,
    settings: Settings,
    session_factory,
    instruction: str,
) -> None:
    if not instruction:
        await message.answer("Использование: /profile_update <что изменить>")
        return
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    # Durable job + быстрый ACK: LLM/merge выполняет фоновый worker (ТЗ §14/§68).
    await enqueue_profile_update(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        instruction=instruction,
    )
    log.info("profile update queued user_id=%s", user_id)
    await message.answer("Принял. Обновляю профиль…")
