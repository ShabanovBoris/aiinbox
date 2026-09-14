import logging
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.domain.enums import SourceType
from app.services.actions import apply_item_action, record_item_events
from app.services.ingestion import ingest_message, ingest_voice
from app.services.notifications import (
    format_settings,
    get_notification_settings,
    update_notification_settings,
)
from app.services.profile import enqueue_profile_update
from app.services.retrieval import (
    TodayService,
    list_categories,
    list_category_items,
    list_inbox,
    search_items,
)

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Personal AI Inbox — отправь текст, URL, voice/audio, YouTube или видео.\n\n"
    "Команды:\n"
    "/today — приоритетные Items на сегодня\n"
    "/inbox — активные Items\n"
    "/search <текст> — поиск по сохранённому содержимому\n"
    "/category [имя] — категории и Items категории\n"
    "/profile — текущий профиль\n"
    "/profile_update <инструкция> — обновить профиль\n"
    "/settings — настройки digest и quiet hours\n"
    "/help — эта справка"
)


async def on_start(message: Message, settings: Settings) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer("Personal AI Inbox готов. Просто отправь текст или ссылку.")


async def on_help(message: Message, settings: Settings) -> None:
    """Expose the stable Telegram command surface without business logic."""
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(HELP_TEXT)


async def on_settings(
    message: Message, settings: Settings, session_factory, arguments: str = ""
) -> None:
    """Show or minimally update notification settings without a settings app."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    from app.services.ingestion import get_or_create_user

    if await get_notification_settings(session_factory, user_id) is None:
        async with session_factory() as session:
            await get_or_create_user(
                session,
                telegram_user_id=user_id,
                chat_id=message.chat.id,
                timezone=settings.default_timezone,
            )
            await session.commit()
    parts = arguments.split(maxsplit=2)
    try:
        if parts and parts[0] == "timezone" and len(parts) == 2:
            result = await update_notification_settings(session_factory, user_id, timezone=parts[1])
        elif parts and parts[0] == "time" and len(parts) == 2:
            result = await update_notification_settings(
                session_factory, user_id, daily_digest_time=parts[1]
            )
        elif parts and parts[0] == "quiet" and len(parts) == 2:
            quiet_parts = parts[1].split("-", maxsplit=1)
            if len(quiet_parts) != 2:
                raise ValueError("Тихие часы: /settings quiet 22:30-08:00")
            result = await update_notification_settings(
                session_factory,
                user_id,
                quiet_hours_start=quiet_parts[0],
                quiet_hours_end=quiet_parts[1],
            )
        elif parts:
            await message.answer("Использование: /settings [timezone|time|quiet] <значение>")
            return
        else:
            result = await get_notification_settings(session_factory, user_id)
        if result is None:
            return
    except ValueError as exc:
        await message.answer(str(exc))
        return
    user, notification_settings = result
    from app.bot.keyboards import settings_keyboard

    await message.answer(
        format_settings(user, notification_settings),
        reply_markup=settings_keyboard(notification_settings["daily_digest_enabled"]),
    )


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
        default_timezone=settings.default_timezone,
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
        default_timezone=settings.default_timezone,
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

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await on_help(message, settings)

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

    @router.message(Command("settings"))
    async def settings_command(message: Message) -> None:
        arguments = (message.text or "").removeprefix("/settings").strip()
        await on_settings(message, settings, session_factory, arguments)

    @router.message(Command("profile_update"))
    async def profile_update(message: Message) -> None:
        if not settings.is_allowed(message.from_user.id if message.from_user else None):
            return
        instruction = (message.text or "").removeprefix("/profile_update").strip()
        await on_profile_update(message, settings, session_factory, instruction)

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        await on_today(message, settings, session_factory)

    @router.message(Command("inbox"))
    async def inbox(message: Message) -> None:
        await on_inbox(message, settings, session_factory)

    @router.message(Command("category"))
    async def category(message: Message) -> None:
        value = (message.text or "").removeprefix("/category").strip()
        await on_category(message, settings, session_factory, value)

    @router.message(Command("search"))
    async def search(message: Message) -> None:
        value = (message.text or "").removeprefix("/search").strip()
        await on_search(message, settings, session_factory, value)

    @router.callback_query(F.data.startswith("item:"))
    async def item_action(callback: CallbackQuery) -> None:
        await on_item_callback(callback, settings, session_factory)

    @router.callback_query(F.data == "settings:digest")
    async def settings_digest(callback: CallbackQuery) -> None:
        await on_settings_callback(callback, settings, session_factory)

    return router


async def on_item_callback(
    callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Translate an inline callback into one scoped application action."""
    user = callback.from_user
    if not settings.is_allowed(user.id) or not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) < 3 or parts[0] != "item":
        await callback.answer("Неизвестное действие")
        return
    action, raw_item_id = parts[1], parts[2]
    try:
        item_id = int(raw_item_id)
    except ValueError:
        await callback.answer("Некорректный Item")
        return
    if action == "later":
        from app.bot.keyboards import snooze_keyboard

        if callback.message:
            await callback.message.edit_text(
                "Когда напомнить?", reply_markup=snooze_keyboard(item_id)
            )
        await callback.answer()
        return
    if action == "snooze" and len(parts) == 4:
        durations = {
            "tomorrow": timedelta(days=1),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
        }
        duration = durations.get(parts[3])
        if duration is None:
            await callback.answer("Некорректный срок")
            return
        action_name = "snooze"
        snoozed_until = datetime.now(UTC).replace(tzinfo=None) + duration
    elif action == "cancel":
        action_name, snoozed_until = "cancel_snooze", None
    elif action in {"done", "archive", "retry"}:
        action_name, snoozed_until = action, None
    else:
        await callback.answer("Неизвестное действие")
        return

    item = await apply_item_action(
        session_factory, user.id, item_id, action_name, snoozed_until=snoozed_until
    )
    if item is None:
        await callback.answer("Item не найден")
        return
    labels = {
        "done": "Готово ✅",
        "archive": "В архиве 🗄",
        "snooze": "Отложено ⏰",
        "cancel_snooze": "Отложенное действие отменено",
        "retry": "Повторно поставил в обработку 🔁",
    }
    if callback.message:
        await callback.message.edit_text(labels[action_name], reply_markup=None)
    await callback.answer()


async def on_settings_callback(
    callback: CallbackQuery, settings: Settings, session_factory
) -> None:
    """Toggle only the safe, single-tap setting and redraw the compact UI."""
    if not settings.is_allowed(callback.from_user.id):
        await callback.answer()
        return
    current = await get_notification_settings(session_factory, callback.from_user.id)
    if current is None:
        await callback.answer("Пользователь не найден")
        return
    user, values = current
    updated = await update_notification_settings(
        session_factory,
        callback.from_user.id,
        daily_digest_enabled=not values["daily_digest_enabled"],
    )
    if callback.message and updated is not None:
        from app.bot.keyboards import settings_keyboard

        updated_user, updated_values = updated
        await callback.message.edit_text(
            format_settings(updated_user, updated_values),
            reply_markup=settings_keyboard(updated_values["daily_digest_enabled"]),
        )
    await callback.answer()


async def on_profile(message: Message, settings: Settings, session_factory) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    from app.bot.formatting import format_profile
    from app.services.ingestion import get_or_create_user
    from app.services.profile import get_profile

    async with session_factory() as session:
        # get_or_create + get_profile: lazy seed работает и для первого /profile
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        profile = await get_profile(session, user.id)
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
        default_timezone=settings.default_timezone,
    )
    log.info("profile update queued user_id=%s", user_id)
    await message.answer("Принял. Обновляю профиль…")


async def _allowed(message: Message, settings: Settings) -> bool:
    return settings.is_allowed(message.from_user.id if message.from_user else None)


async def on_today(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_today
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        items = await TodayService().list_items(session, user.id)
        await record_item_events(session, user.id, [item.id for item in items], "TODAY_SHOWN")
        await session.commit()
    await message.answer(format_today(items))


async def on_inbox(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        items = await list_inbox(session, user.id)
    await message.answer(format_item_list(items, "Входящие:"))


async def on_category(message: Message, settings: Settings, session_factory, category: str) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_categories, format_item_list
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        if category:
            items = await list_category_items(session, user.id, category)
            response = format_item_list(items, f"Категория: {category}")
        else:
            response = format_categories(await list_categories(session, user.id))
    await message.answer(response)


async def on_search(message: Message, settings: Settings, session_factory, query: str) -> None:
    if not await _allowed(message, settings):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    if not query:
        await message.answer("Использование: /search <запрос>")
        return
    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            timezone=settings.default_timezone,
        )
        items = await search_items(session, user.id, query)
    await message.answer(format_item_list(items, "Результаты поиска:"))
