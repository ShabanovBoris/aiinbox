import logging
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.formatting import (
    format_attention_item,
    format_item_details,
    format_item_failure,
    format_ready_item_compact,
    format_weekly_review,
)
from app.bot.keyboards import (
    attention_chooser_keyboard,
    attention_settings_keyboard,
    attention_status_keyboard,
    category_navigation_keyboard,
    export_chooser_keyboard,
    feedback_category_keyboard,
    feedback_menu_keyboard,
    feedback_type_keyboard,
    help_keyboard,
    input_cancel_keyboard,
    item_details_keyboard,
    item_interest_keyboard,
    item_keyboard,
    item_list_keyboard,
    item_more_keyboard,
    item_sources_keyboard,
    main_menu_keyboard,
    proactive_reminder_keyboard,
    profile_keyboard,
    reminder_more_keyboard,
    reminder_snooze_keyboard,
    reminder_sources_keyboard,
    search_empty_keyboard,
    settings_keyboard,
)
from app.bot.presentation import item_display_title, item_navigation_entries
from app.bot.provenance import normalize_forward_origin, text_with_entity_urls
from app.config import Settings
from app.domain.category_tokens import category_token
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.extractors.document import document_format_hint, safe_document_file_name
from app.extractors.instagram import is_instagram_reel_url
from app.services.actions import apply_item_action, record_item_events, set_item_interest
from app.services.ask_inbox import MAX_ASK_QUESTION_CHARS, enqueue_ask
from app.services.attention_ranking import AttentionRankingService
from app.services.delivery import enqueue_item_video_delivery
from app.services.export import COMPACT, FULL, enqueue_export
from app.services.feedback import (
    correct_item_category_by_token,
    correct_item_type,
    record_item_feedback,
)
from app.services.ingestion import ingest_media, ingest_message
from app.services.motivation import MOTIVATION_NUDGE
from app.services.notifications import (
    format_attention_settings,
    format_attention_status,
    format_settings,
    get_attention_status,
    get_notification_settings,
    parse_timezone,
    update_notification_settings,
)
from app.services.original_access import item_original_target, reminder_original_target
from app.services.profile import enqueue_profile_update
from app.services.reminder_feedback import ReminderFeedbackService
from app.services.retrieval import (
    TodayService,
    list_categories_page,
    list_category_items_page,
    list_inbox_page,
    load_item_sources_by_item,
    resolve_category_token,
    search_items,
)
from app.services.url_parsing import find_urls
from app.services.weekly_review import WeeklyReviewService
from app.storage.models import Item, ItemSource, User

log = logging.getLogger(__name__)

# ❌ Удалена старая HELP_TEXT-командная простыня: основные действия теперь видны в inline-меню.
HELP_TEXT = (
    "Просто отправь\n"
    "• текст, ссылку, видео, документ или пересланное сообщение\n\n"
    "Найти\n"
    "• 🔎 Поиск по словам в сохранённом\n"
    "• 🧠 Спросить — ответ по найденным сохранённым материалам\n\n"
    "Вернуться\n"
    "• 🎯 Сегодня\n"
    "• ✨ Внимание\n"
    "• 📊 Неделя\n\n"
    "Управлять\n"
    "• 👤 Профиль\n"
    "• ⚙️ Настройки\n"
    "\nЭкспорт\n"
    "• Компактный — сохранённое, источники, профиль и история\n"
    "• Полный — дополнительно тексты и транскрипты\n\n"
    "Команды с / тоже работают."
)


class GuidedInput(StatesGroup):
    """One-shot Telegram input; durable business operations remain in their workers."""

    ask = State()
    search = State()
    profile = State()
    settings_timezone = State()
    settings_digest_time = State()
    settings_quiet_hours = State()


_ASK_PROMPT = (
    "🧠 Ответ по найденным сохранённым материалам.\n\n"
    "Напиши вопрос одним сообщением. Например: «Что я сохранял про Kotlin?»"
)
_SEARCH_PROMPT = (
    "🔎 Поиск по словам\n\n"
    "Ищу совпадения в заголовках, сводках, заметках и сохранённом тексте.\n\n"
    "Поиск текстовый, не смысловой: синонимы, перевод и разные написания могут не совпасть.\n\n"
    "Напиши запрос."
)


async def _begin_guided_input(
    state: FSMContext,
    input_state: State,
    send,
    prompt: str,
) -> None:
    """FSM routes only the next reply; durable work stays in existing services.

    State is armed after Telegram accepts the prompt, so a failed edit cannot leave
    the next ordinary message captured by an invisible guided flow.
    """
    await send(prompt, reply_markup=input_cancel_keyboard())
    await state.set_state(input_state)


class ClearGuidedInputOnCommandMiddleware(BaseMiddleware):
    """Let ordinary commands escape one-shot text prompts without capturing their text."""

    async def __call__(self, handler, event, data):
        if (
            isinstance(event, Message)
            and event.text
            and event.text.startswith("/")
            and event.forward_origin is None
        ):
            state = data.get("state")
            if state is not None and await state.get_state() is not None:
                await state.clear()
        return await handler(event, data)


class ClearGuidedInputOnCallbackMiddleware(BaseMiddleware):
    """Clear stale text prompts when another inline action takes over the chat."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.data not in {
            "nav:ask",
            "nav:search",
            "nav:input:cancel",
        }:
            state = data.get("state")
            if state is not None and await state.get_state() is not None:
                await state.clear()
        return await handler(event, data)


async def on_start(message: Message, settings: Settings) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "AIInbox готов.\n\nОтправь текст, ссылку, видео или документ — либо выбери действие ниже.",
        reply_markup=main_menu_keyboard(),
    )


async def on_help(message: Message, settings: Settings) -> None:
    """Expose the stable Telegram command surface without business logic."""
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(HELP_TEXT, reply_markup=help_keyboard())


async def on_menu(message: Message, settings: Settings) -> None:
    """Return the authorized user to the compact navigation projection."""
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())


# ❌ Удалена раздельная сборка read-only экранов настроек: slash и callbacks теперь
# строят текст и клавиатуру из одной проекции текущих настроек.
async def _build_settings_projection(
    telegram_user_id: int,
    chat_id: int,
    settings: Settings,
    session_factory,
    *,
    attention: bool = False,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Project canonical notification settings for both slash and inline entry points."""
    if not settings.is_allowed(telegram_user_id):
        return None
    result = await get_notification_settings(session_factory, telegram_user_id)
    if result is None:
        from app.services.ingestion import get_or_create_user

        async with session_factory() as session:
            await get_or_create_user(
                session,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                timezone=settings.default_timezone,
            )
            await session.commit()
        result = await get_notification_settings(session_factory, telegram_user_id)
    if result is None:
        return None
    user, values = result
    if attention:
        return (
            format_attention_settings(values),
            attention_settings_keyboard(
                values["attention_enabled"],
                values["attention_intensity"],
                values["generic_motivation_enabled"],
            ),
        )
    from app.bot.keyboards import settings_keyboard

    return (
        format_settings(user, values),
        settings_keyboard(values["daily_digest_enabled"]),
    )


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
        if parts and parts[0] == "attention" and len(parts) == 1:
            projection = await _build_settings_projection(
                user_id, message.chat.id, settings, session_factory, attention=True
            )
            if projection is None:
                return
            await message.answer(projection[0], reply_markup=projection[1])
            return
        elif parts and parts[0] == "timezone" and len(parts) == 2:
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
            await message.answer(
                "Использование: /settings [timezone|time|quiet] <значение> или /settings attention"
            )
            return
        else:
            projection = await _build_settings_projection(
                user_id, message.chat.id, settings, session_factory
            )
            if projection is not None:
                await message.answer(projection[0], reply_markup=projection[1])
            return
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if result is None:
        return
    user, notification_settings = result
    from app.bot.keyboards import settings_keyboard

    await message.answer(
        format_settings(user, notification_settings),
        reply_markup=settings_keyboard(notification_settings["daily_digest_enabled"]),
    )


async def on_settings_edit_callback(
    callback: CallbackQuery,
    settings: Settings,
    session_factory,
    state: FSMContext,
) -> None:
    """Keep editing ephemeral and delegate validation/persistence to notification services."""
    if (
        not settings.is_allowed(callback.from_user.id)
        or not callback.data
        or callback.message is None
    ):
        await callback.answer()
        return
    choices = {
        "settings:edit:timezone": (
            GuidedInput.settings_timezone,
            "🌍 Часовой пояс\nОтправь его название, например Europe/Moscow.",
        ),
        "settings:edit:digest_time": (
            GuidedInput.settings_digest_time,
            "🕘 Время подборки\nОтправь время в формате HH:MM, например 09:00.",
        ),
        "settings:edit:quiet_hours": (
            GuidedInput.settings_quiet_hours,
            "🌙 Тихие часы\nОтправь диапазон HH:MM-HH:MM, например 22:30-08:00.",
        ),
    }
    choice = choices.get(callback.data)
    if choice is None:
        await callback.answer("Настройка больше недоступна")
        return
    input_state, prompt = choice
    await _begin_guided_input(
        state,
        input_state,
        callback.message.edit_text,
        prompt,
    )
    await callback.answer()


async def on_guided_notification_setting_input(
    message: Message,
    settings: Settings,
    session_factory,
    state: FSMContext,
    setting_name: str,
) -> None:
    """Validate through the notification service and retain state after invalid input."""
    telegram_user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(telegram_user_id):
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value:
        return
    try:
        if setting_name == "timezone":
            updated = await update_notification_settings(
                session_factory, telegram_user_id, timezone=value
            )
        elif setting_name == "digest_time":
            updated = await update_notification_settings(
                session_factory, telegram_user_id, daily_digest_time=value
            )
        elif setting_name == "quiet_hours":
            times = value.split("-", maxsplit=1)
            if len(times) != 2:
                raise ValueError("Диапазон должен быть в формате HH:MM-HH:MM")
            updated = await update_notification_settings(
                session_factory,
                telegram_user_id,
                quiet_hours_start=times[0].strip(),
                quiet_hours_end=times[1].strip(),
            )
        else:
            raise ValueError("Неизвестная настройка")
    except ValueError as exc:
        await message.answer(f"{exc}\nПопробуй ещё раз или нажми «Отмена».")
        return
    if updated is None:
        await message.answer("Настройки не найдены. Открой их повторно через меню.")
        return
    await state.clear()
    user, values = updated
    await message.answer(
        format_settings(user, values),
        reply_markup=settings_keyboard(values["daily_digest_enabled"]),
    )


async def on_text(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        # Allowlist: неавторизованным молча не отвечаем и ничего не обрабатываем.
        log.warning("unauthorized telegram user ignored user_id=%s", user_id)
        return
    text = text_with_entity_urls(message.text, message.entities)
    if not text:
        return
    # Тяжёлая обработка запрещена в handler: только валидация, Item QUEUED и ответ.
    # Сначала persistence, потом ACK: при ошибке БД пользователь не получает
    # ложное подтверждение сохранения.
    result = await ingest_message(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        source_metadata=normalize_forward_origin(message.forward_origin),
        default_timezone=settings.default_timezone,
    )
    ack_lines = []
    if result.items:
        ack_lines.append(
            "Принял Reel. Разбираю…"
            if any(is_instagram_reel_url(url) for url in find_urls(text))
            else "Принял. Разбираю…"
        )
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
    source_metadata = normalize_forward_origin(message.forward_origin)
    source_text = text_with_entity_urls(message.caption, message.caption_entities)
    result = await ingest_media(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=media.file_id,
        duration_seconds=media.duration,
        source_type=source_type,
        too_large=oversized,
        source_metadata=source_metadata,
        source_text=source_text,
        default_timezone=settings.default_timezone,
    )
    item = result.items[0]
    if item.processing_status is ProcessingStatus.FAILED and item.error_code == "TOO_LARGE":
        limit_mb = max_audio_bytes / 1_000_000
        await message.answer(
            f"Файл слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{limit_mb:.0f} МБ). Метаданные сохранил — файл не скачан."
        )
        return
    if oversized is not None:
        limit_mb = max_audio_bytes / 1_000_000
        await message.answer(
            f"Файл слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{limit_mb:.0f} МБ), но текст и ссылки из сообщения разберу."
        )
        return
    label = "аудио" if is_audio else "голосовое"
    await message.answer(f"Принял {label}. Разбираю…")


async def on_video(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Persist Telegram video, caption and caption URLs as one composite Item."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    media = message.video
    if media is None and _is_video_document(message):
        media = message.document
    if media is None:
        return
    file_size = media.file_size or 0
    oversized = (
        (file_size, settings.max_video_bytes) if file_size > settings.max_video_bytes else None
    )
    source_text = text_with_entity_urls(message.caption, message.caption_entities)
    result = await ingest_media(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=media.file_id,
        duration_seconds=getattr(media, "duration", None),
        source_type=SourceType.VIDEO,
        too_large=oversized,
        source_metadata=normalize_forward_origin(message.forward_origin),
        source_text=source_text,
        default_timezone=settings.default_timezone,
    )
    item = result.items[0]
    if item.processing_status is ProcessingStatus.FAILED and item.error_code == "TOO_LARGE":
        await message.answer(
            f"Видео слишком большое ({file_size / 1_000_000:.1f} МБ > лимита "
            f"{settings.max_video_bytes / 1_000_000:.0f} МБ)."
        )
        return
    if oversized is not None:
        await message.answer("Видео превышает лимит, но текст и ссылки из сообщения разберу.")
        return
    await message.answer("Принял видео. Разбираю текст, ссылки и видео…")


def _is_video_document(message: Message) -> bool:
    """Normalize Telegram's alternate transport for videos into the VIDEO source path.

    Telegram may expose a forwarded/uploaded video as Document instead of Video;
    MIME and common video suffixes are transport signals, while downstream
    extraction remains identical and still produces one media ItemSource.
    """
    document = message.document
    if document is None:
        return False
    mime_type = (document.mime_type or "").lower()
    file_name = (document.file_name or "").lower()
    return mime_type.startswith("video/") or file_name.endswith(
        (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi")
    )


async def on_forwarded_photo(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Analyze forwarded photo caption/links even while image understanding is unavailable."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    text = text_with_entity_urls(message.caption, message.caption_entities)
    if not text:
        await message.answer("Пересланные фото без поддерживаемой ссылки пока не поддерживаются.")
        return
    result = await ingest_message(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        source_metadata=normalize_forward_origin(message.forward_origin),
        default_timezone=settings.default_timezone,
    )
    ack_lines = []
    if result.items:
        ack_lines.append("Принял пересланное сообщение. Текст и ссылки разбираю…")
    if result.duplicates:
        ack_lines.append("Часть ссылок уже сохранена — дубли пропустил.")
    if ack_lines:
        await message.answer("\n".join(ack_lines))


# ❌ Удалена forwarded-only ветка отказа: документы обоих видов проходят общую проверку и pipeline.
async def on_unsupported_document(message: Message, settings: Settings) -> None:
    """Give the same concise format error for unsupported direct and forwarded files."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    await message.answer("Этот формат документа пока не поддерживается.")


async def on_document(
    message: Message, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Persist supported documents as one file ItemSource; workers do all parsing."""
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    document = message.document
    if document is None:
        return
    file_name = safe_document_file_name(document.file_name)
    document_format = document_format_hint(file_name, document.mime_type)
    file_size = document.file_size or 0
    oversized = (
        (file_size, settings.max_document_bytes)
        if file_size > settings.max_document_bytes
        else None
    )
    source_text = text_with_entity_urls(message.caption, message.caption_entities)
    source_details = {
        key: value
        for key, value in {
            "file_name": file_name,
            "mime_type": (document.mime_type or "").split(";", 1)[0].strip().lower() or None,
            "document_format": document_format,
        }.items()
        if value is not None
    }
    result = await ingest_media(
        session_factory,
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=document.file_id,
        duration_seconds=None,
        source_type=SourceType.DOCUMENT,
        too_large=oversized,
        prechecked_failure=(
            ("UNSUPPORTED_SOURCE", "document format is unsupported")
            if document_format is None
            else None
        ),
        source_metadata=normalize_forward_origin(message.forward_origin),
        source_text=source_text,
        source_details=source_details,
        default_timezone=settings.default_timezone,
    )
    item = result.items[0]
    if oversized is not None:
        if item.processing_status is ProcessingStatus.FAILED:
            answer = (
                f"Документ слишком большой ({file_size / 1_000_000:.1f} МБ > лимита "
                f"{settings.max_document_bytes / 1_000_000:.0f} МБ). Файл не скачан."
            )
        else:
            answer = (
                f"Документ превышает лимит {settings.max_document_bytes / 1_000_000:.0f} МБ; "
                "разберу подпись и ссылки из сообщения."
            )
        await message.answer(answer)
        return
    if document_format is None:
        await on_unsupported_document(message, settings)
        return
    label = file_name or "без имени"
    await message.answer(f"Принял документ {label}. Разбираю…")


def make_router(
    settings: Settings, session_factory: async_sessionmaker, max_audio_bytes: int = 20_000_000
) -> Router:
    router = Router()
    router.message.outer_middleware(ClearGuidedInputOnCommandMiddleware())
    router.callback_query.outer_middleware(ClearGuidedInputOnCallbackMiddleware())

    # Specific media handlers must precede the forwarded-text catch-all. Aiogram's
    # MagicFilter field lookup is permissive enough that relying on F.text alone for
    # dispatch priority can let a media update reach the wrong callback.
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

    @router.message(F.video)
    async def video(message: Message) -> None:
        await on_video(message, settings, session_factory)

    @router.message(F.forward_origin, F.photo)
    async def forwarded_photo(message: Message) -> None:
        await on_forwarded_photo(message, settings, session_factory)

    @router.message(F.document)
    async def document(message: Message) -> None:
        # Telegram sometimes serializes ordinary/forwarded video as Document.
        # Normalize it here so message transport does not change Item semantics.
        if _is_video_document(message):
            await on_video(message, settings, session_factory)
        else:
            await on_document(message, settings, session_factory)

    # Forwarded slash-prefixed text is captured content, not a command for this bot.
    # This edge rule must run before Command filters to preserve author semantics.
    @router.message(F.forward_origin, F.text)
    async def forwarded_text(message: Message) -> None:
        await on_text(message, settings, session_factory)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await on_start(message, settings)

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await on_help(message, settings)

    @router.message(Command("menu"))
    async def menu_command(message: Message) -> None:
        await on_menu(message, settings)

    @router.message(GuidedInput.ask, ~F.forward_origin, F.text, ~F.text.startswith("/"))
    async def guided_ask(message: Message, state: FSMContext) -> None:
        await on_guided_ask_input(message, settings, session_factory, state)

    @router.message(GuidedInput.profile, ~F.forward_origin, F.text, ~F.text.startswith("/"))
    async def guided_profile(message: Message, state: FSMContext) -> None:
        await on_guided_profile_input(message, settings, session_factory, state)

    @router.message(GuidedInput.search, ~F.forward_origin, F.text, ~F.text.startswith("/"))
    async def guided_search(message: Message, state: FSMContext) -> None:
        await on_guided_search_input(message, settings, session_factory, state)

    @router.message(
        GuidedInput.settings_timezone, ~F.forward_origin, F.text, ~F.text.startswith("/")
    )
    async def guided_timezone(message: Message, state: FSMContext) -> None:
        """Keep this validated setting reply ahead of ordinary text ingestion."""
        await on_guided_notification_setting_input(
            message, settings, session_factory, state, "timezone"
        )

    @router.message(
        GuidedInput.settings_digest_time, ~F.forward_origin, F.text, ~F.text.startswith("/")
    )
    async def guided_digest_time(message: Message, state: FSMContext) -> None:
        """Route digest-clock input into the shared notification validator."""
        await on_guided_notification_setting_input(
            message, settings, session_factory, state, "digest_time"
        )

    @router.message(
        GuidedInput.settings_quiet_hours, ~F.forward_origin, F.text, ~F.text.startswith("/")
    )
    async def guided_quiet_hours(message: Message, state: FSMContext) -> None:
        """Route quiet-hours input into the shared notification validator."""
        await on_guided_notification_setting_input(
            message, settings, session_factory, state, "quiet_hours"
        )

    # Не-командный текст — источники TEXT/WEB; медиа-источники добавляются
    # в своих фазах и идут через тот же pipeline.
    @router.message(~F.forward_origin, F.text, ~F.text.startswith("/"))
    async def text(message: Message) -> None:
        await on_text(message, settings, session_factory)

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
    async def profile_update(message: Message, state: FSMContext, command: CommandObject) -> None:
        if not settings.is_allowed(message.from_user.id if message.from_user else None):
            return
        instruction = command.args or ""
        await on_profile_update(message, settings, session_factory, instruction, state)

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        await on_today(message, settings, session_factory)

    @router.message(Command("attention"))
    async def attention(message: Message) -> None:
        """Translate the Telegram command into the PM-07 preview application call."""
        parts = (message.text or "").split(maxsplit=1)
        arguments = parts[1] if len(parts) == 2 else ""
        await on_attention(message, settings, session_factory, arguments)

    @router.message(Command("weekly"))
    async def weekly(message: Message) -> None:
        await on_weekly(message, settings, session_factory)

    @router.message(Command("inbox"))
    async def inbox(message: Message) -> None:
        await on_inbox(message, settings, session_factory)

    @router.message(Command("category"))
    async def category(message: Message) -> None:
        value = (message.text or "").removeprefix("/category").strip()
        await on_category(message, settings, session_factory, value)

    @router.message(Command("search"))
    async def search(message: Message, state: FSMContext, command: CommandObject) -> None:
        value = command.args or ""
        await on_search_with_state(message, settings, session_factory, value, state)

    @router.message(Command("ask"))
    async def ask(message: Message, state: FSMContext) -> None:
        parts = (message.text or "").split(maxsplit=1)
        question = parts[1].strip() if len(parts) == 2 else ""
        await on_ask(message, settings, session_factory, question, state)

    @router.message(Command("export"))
    async def export(message: Message) -> None:
        await on_export(message, settings, session_factory)

    @router.callback_query(F.data.startswith("nav:"))
    async def navigation(callback: CallbackQuery, state: FSMContext) -> None:
        await on_navigation_callback(callback, settings, session_factory, state)

    @router.callback_query(F.data.startswith("export:mode:"))
    async def export_mode(callback: CallbackQuery) -> None:
        await on_export_mode_callback(callback, settings, session_factory)

    @router.callback_query(F.data.startswith("item:"))
    async def item_action(callback: CallbackQuery) -> None:
        await on_item_callback(callback, settings, session_factory)

    @router.callback_query(F.data.startswith("reminder:"))
    async def reminder_action(callback: CallbackQuery) -> None:
        await on_reminder_callback(callback, settings, session_factory)

    @router.callback_query(F.data.startswith("feedback:"))
    async def item_feedback(callback: CallbackQuery) -> None:
        await on_feedback_callback(callback, settings, session_factory)

    @router.callback_query(F.data == "settings:digest")
    async def settings_digest(callback: CallbackQuery) -> None:
        await on_settings_callback(callback, settings, session_factory)

    @router.callback_query(F.data.startswith("settings:edit:"))
    async def settings_edit(callback: CallbackQuery, state: FSMContext) -> None:
        """Translate one settings button into a bounded, one-shot input state."""
        await on_settings_edit_callback(callback, settings, session_factory, state)

    @router.callback_query(F.data == "settings:open")
    async def settings_open(callback: CallbackQuery) -> None:
        await on_settings_open_callback(callback, settings, session_factory)

    @router.callback_query(F.data.startswith("settings:attention:"))
    async def settings_attention(callback: CallbackQuery) -> None:
        await on_attention_settings_callback(callback, settings, session_factory)

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
        await callback.answer("Некорректное сохранение")
        return
    if item_id < 1:
        await callback.answer("Некорректное сохранение")
        return
    current_chat_id = getattr(getattr(callback.message, "chat", None), "id", None)

    if action == "original":
        if len(parts) != 3:
            await callback.answer("Некорректное действие")
            return
        target = await item_original_target(
            session_factory,
            user.id,
            item_id,
            current_chat_id=current_chat_id,
        )
        if target is None:
            await callback.answer("Сохранение недоступно")
            return
        try:
            await callback.bot.copy_message(
                chat_id=target.destination_chat_id,
                from_chat_id=target.source_chat_id,
                message_id=target.message_id,
            )
        except TelegramBadRequest as exc:
            if not _original_is_unavailable(exc):
                raise
            projection = await _load_item_ui_projection(
                session_factory,
                user.id,
                item_id,
                current_chat_id=current_chat_id,
            )
            if projection is None:
                await callback.answer("Сохранение недоступно")
                return
            item, sources, _ = projection
            source_markup = item_sources_keyboard(item, sources)
            has_source_actions = len(source_markup.inline_keyboard) > 1
            unavailable_text = "Оригинальное сообщение больше недоступно."
            if has_source_actions:
                unavailable_text += "\nМожно открыть сохранённый источник:"
            if callback.message is not None:
                await callback.message.answer(
                    unavailable_text,
                    reply_markup=source_markup if has_source_actions else None,
                )
            await callback.answer("Оригинал недоступен")
            return
        await callback.answer("Оригинал отправлен")
        return

    if action in {"view", "more", "details", "sources", "interest_menu", "back"}:
        if len(parts) != 3:
            await callback.answer("Некорректное действие")
            return
        if action == "view" and current_chat_id is None:
            await callback.answer("Сохранение недоступно")
            return
        projection = await _load_item_ui_projection(
            session_factory,
            user.id,
            item_id,
            current_chat_id=current_chat_id if action == "view" else None,
        )
        if projection is None:
            await callback.answer("Сохранение недоступно")
            return
        item, sources, original_available = projection
        if action == "view":
            if item.processing_status is ProcessingStatus.READY:
                text = format_ready_item_compact(item, sources)
            elif item.processing_status is ProcessingStatus.FAILED:
                text = format_item_failure(item, sources)
            else:
                text = f"⏳ Обрабатывается: {item_display_title(item, sources)}"
            if callback.message is not None:
                await callback.message.answer(
                    text,
                    reply_markup=item_keyboard(
                        item,
                        sources,
                        original_available=original_available,
                    ),
                )
        elif action == "more":
            await _edit_reply_markup_if_changed(callback.message, item_more_keyboard(item))
        elif action == "interest_menu":
            if item.processing_status is not ProcessingStatus.READY or item.state not in {
                ItemState.ACTIVE,
                ItemState.SNOOZED,
            }:
                await callback.answer("Сохранение недоступно")
                return
            await _edit_reply_markup_if_changed(callback.message, item_interest_keyboard(item))
        elif action == "details":
            if item.processing_status is not ProcessingStatus.READY:
                await callback.answer("Детали пока недоступны")
                return
            await _edit_item_message_if_changed(
                callback.message,
                format_item_details(item),
                item_details_keyboard(item.id),
            )
        elif action == "sources":
            await _edit_reply_markup_if_changed(
                callback.message, item_sources_keyboard(item, sources)
            )
        else:
            if item.processing_status is ProcessingStatus.READY:
                text = format_ready_item_compact(item, sources)
            elif item.processing_status is ProcessingStatus.FAILED:
                text = format_item_failure(item, sources)
            else:
                text = "Сохранение пока обрабатывается."
            await _edit_item_message_if_changed(
                callback.message,
                text,
                item_keyboard(item, sources, original_available=original_available),
            )
        await callback.answer()
        return

    if action == "video":
        if len(parts) != 4:
            await callback.answer("Некорректный источник видео")
            return
        try:
            source_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный источник видео")
            return
        result = await enqueue_item_video_delivery(
            session_factory,
            user.id,
            item_id,
            source_id,
        )
        if result is None:
            await callback.answer("Это видео сейчас недоступно")
        elif result == "IN_PROGRESS":
            await callback.answer("Видео уже готовится")
        else:
            await callback.answer("Поставил видео в очередь")
        return
    if action == "interest":
        if len(parts) != 4:
            await callback.answer("Некорректный уровень интереса")
            return
        try:
            level = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный уровень интереса")
            return
        if level not in {1, 2, 3}:
            await callback.answer("Некорректный уровень интереса")
            return
        projection = await _load_item_ui_projection(
            session_factory,
            user.id,
            item_id,
            current_chat_id=current_chat_id,
        )
        if projection is None or projection[0].processing_status is not ProcessingStatus.READY:
            await callback.answer("Сохранение недоступно")
            return
        if projection[0].state not in {ItemState.ACTIVE, ItemState.SNOOZED}:
            await callback.answer("Сохранение недоступно")
            return
        result = await set_item_interest(session_factory, user.id, item_id, level)
        if result is None:
            await callback.answer("Сохранение недоступно")
            return
        item, changed = result
        # ❌ Удалён возврат к полной карточке после смены интереса: submenu сохраняет
        # контекст, а обновлённый чекмарк строится из canonical Item.
        await _edit_reply_markup_if_changed(callback.message, item_interest_keyboard(item))
        await callback.answer("Интерес обновлён" if changed else "Уже выбран этот уровень")
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
        await callback.answer("Сохранение не найдено")
        return
    # ❌ Удален label lookup только по requested action: при concurrent CAS он
    # мог подтверждать проигравшее действие вместо persisted результата.
    if callback.message:
        await callback.message.edit_text(_item_action_label(item, action_name), reply_markup=None)
    await callback.answer()


async def on_reminder_callback(
    callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Translate reminder identity/actions into the ReminderFeedbackService boundary."""
    if not settings.is_allowed(callback.from_user.id) or not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) not in {3, 4} or parts[0] != "reminder":
        await callback.answer("Некорректное действие")
        return
    try:
        reminder_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректное напоминание")
        return
    if reminder_id < 1:
        await callback.answer("Некорректное напоминание")
        return

    callback_action = parts[1]
    service = ReminderFeedbackService(session_factory)
    if callback_action in {"more", "sources", "back"} and len(parts) == 3:
        projection = await service.item_reminder_projection(callback.from_user.id, reminder_id)
        if projection is None:
            await callback.answer("Это напоминание сейчас недоступно")
            return
        reminder, item, sources, original_available = projection
        focus_source_id = (reminder.payload_json or {}).get("focus_source_id")
        if type(focus_source_id) is not int:
            focus_source_id = None
        if callback_action == "more":
            keyboard = reminder_more_keyboard(
                reminder_id, dismiss_available=reminder.type != MOTIVATION_NUDGE
            )
        elif callback_action == "sources":
            keyboard = reminder_sources_keyboard(
                reminder_id, item, sources, focus_source_id=focus_source_id
            )
        else:
            keyboard = proactive_reminder_keyboard(
                reminder_id,
                item,
                sources,
                focus_source_id=focus_source_id,
                original_available=original_available,
            )
        await _edit_reply_markup_if_changed(callback.message, keyboard)
        await callback.answer()
        return

    if callback_action == "original" and len(parts) == 3:
        chat_id = getattr(getattr(callback.message, "chat", None), "id", None)
        target = await reminder_original_target(
            session_factory,
            callback.from_user.id,
            reminder_id,
            current_chat_id=chat_id,
        )
        if target is None:
            await callback.answer("Это напоминание сейчас недоступно")
            return
        try:
            await callback.bot.copy_message(
                chat_id=target.destination_chat_id,
                from_chat_id=target.source_chat_id,
                message_id=target.message_id,
            )
        except TelegramBadRequest as exc:
            if not _original_is_unavailable(exc):
                raise
            projection = await service.item_reminder_projection(callback.from_user.id, reminder_id)
            if projection is not None and callback.message is not None:
                reminder, item, sources, _ = projection
                focus_source_id = (reminder.payload_json or {}).get("focus_source_id")
                if type(focus_source_id) is not int:
                    focus_source_id = None
                keyboard = reminder_sources_keyboard(
                    reminder_id, item, sources, focus_source_id=focus_source_id
                )
                message = "Оригинальное сообщение больше недоступно."
                if len(keyboard.inline_keyboard) > 1:
                    message += "\nМожно открыть сохранённый источник:"
                await callback.message.answer(message, reply_markup=keyboard)
            await callback.answer("Оригинал недоступен")
            return
        await service.apply_callback(
            callback.from_user.id,
            reminder_id,
            "open_original",
            callback_id=callback.id,
        )
        await callback.answer("Оригинал отправлен")
        return

    kwargs = {"callback_id": callback.id}
    if callback_action == "snooze" and len(parts) == 4:
        durations = {
            "tomorrow": timedelta(days=1),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
        }
        duration = durations.get(parts[3])
        if duration is None:
            await callback.answer("Некорректный срок")
            return
        action = "snooze"
        kwargs["snoozed_until"] = datetime.now(UTC).replace(tzinfo=None) + duration
    elif callback_action == "open" and len(parts) == 4:
        try:
            kwargs["source_id"] = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный источник")
            return
        action = "open"
    elif len(parts) == 3:
        action = {
            "later": "later",
            "cancel": "cancel",
            "done": "done",
            "dismiss": "dismiss",
            "less": "dislike",
            "ok": "ok",
        }.get(callback_action)
        if action is None:
            await callback.answer("Неизвестное действие")
            return
    else:
        await callback.answer("Некорректное действие")
        return

    result = await service.apply_callback(
        callback.from_user.id,
        reminder_id,
        action,
        **kwargs,
    )
    if result == "APPLIED" and action == "later":
        if callback.message:
            await _edit_reply_markup_if_changed(
                callback.message, reminder_snooze_keyboard(reminder_id)
            )
        await callback.answer("Выбери срок")
        return
    if result == "APPLIED" and action == "cancel":
        projection = await service.item_reminder_projection(callback.from_user.id, reminder_id)
        if callback.message:
            if projection is None:
                await _edit_reply_markup_if_changed(callback.message, None)
            else:
                reminder, item, sources, original_available = projection
                focus_source_id = (reminder.payload_json or {}).get("focus_source_id")
                if type(focus_source_id) is not int:
                    focus_source_id = None
                await _edit_reply_markup_if_changed(
                    callback.message,
                    proactive_reminder_keyboard(
                        reminder_id,
                        item,
                        sources,
                        focus_source_id=focus_source_id,
                        original_available=original_available,
                    ),
                )
        await callback.answer("Выбор отменён")
        return
    if result == "APPLIED" and action == "ok":
        if callback.message:
            await _edit_reply_markup_if_changed(callback.message, None)
        await callback.answer("Хорошо")
        return
    if result == "APPLIED":
        if action in {"done", "snooze", "dismiss", "dislike"} and callback.message:
            await _edit_reply_markup_if_changed(callback.message, None)
        answers = {
            "done": "Сделано",
            "snooze": "Отложено",
            "dismiss": "Учту время",
            "dislike": "Записал — буду показывать меньше похожих",
        }
        await callback.answer(answers.get(action, "Записал"))
        return
    if result == "QUEUED":
        await callback.answer("Поставил видео в очередь")
        return
    if result == "IN_PROGRESS":
        await callback.answer("Видео уже готовится")
        return
    if result == "ALREADY_DONE":
        if callback.message:
            await _edit_reply_markup_if_changed(callback.message, None)
        await callback.answer("Уже сделано")
        return
    if result == "ALREADY_RECORDED":
        if callback.message:
            await _edit_reply_markup_if_changed(callback.message, None)
        await callback.answer("Уже учтено")
        return
    if result == "DUPLICATE_CALLBACK":
        await callback.answer("Уже учтено")
        return
    await callback.answer("Это напоминание сейчас недоступно")


async def _load_ready_feedback_projection(
    session_factory: async_sessionmaker, telegram_user_id: int, item_id: int
) -> tuple[Item, list[ItemSource]] | None:
    """Keep existing feedback actions restricted to owner-scoped READY Items."""
    projection = await _load_item_ui_projection(session_factory, telegram_user_id, item_id)
    if projection is None or projection[0].processing_status is not ProcessingStatus.READY:
        return None
    return projection[0], projection[1]


async def _load_item_ui_projection(
    session_factory: async_sessionmaker,
    telegram_user_id: int,
    item_id: int,
    *,
    current_chat_id: int | None = None,
) -> tuple[Item, list[ItemSource], bool] | None:
    """Reload an owner's Item only in its persisted Telegram chat when one is supplied."""
    async with session_factory() as session:
        row = await session.execute(
            select(Item, User.telegram_chat_id)
            .join(User, User.id == Item.user_id)
            .where(
                User.telegram_user_id == telegram_user_id,
                Item.id == item_id,
            )
        )
        projection = row.one_or_none()
        if projection is None:
            return None
        item, chat_id = projection
        if chat_id is None or (current_chat_id is not None and chat_id != current_chat_id):
            return None
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item.id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )
        return item, sources, item.telegram_message_id is not None


def _original_is_unavailable(exc: TelegramBadRequest) -> bool:
    """Map only known permanent Bot API copy failures to the deleted-source UX."""
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "message to copy not found",
            "message_id_invalid",
            "message can't be copied",
            "message can not be copied",
            "message can't be forwarded",
            "message can not be forwarded",
        )
    )


async def _edit_reply_markup_if_changed(message, reply_markup) -> None:
    """Treat duplicate markup edits as no-ops while preserving other Telegram errors.

    Callback transport retries can arrive after the DB commit and original edit;
    Telegram's exact "message is not modified" response is the successful
    idempotent projection in that race, while unrelated errors still propagate.
    """
    if (
        message is None
        or not hasattr(message, "edit_reply_markup")
        or getattr(message, "reply_markup", None) == reply_markup
    ):
        return
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise


# ❌ Удалена перерисовка READY-карточки после correction callbacks: изменённые поля
# остаются доступны в Details, а пользователь остаётся в Feedback submenu.
async def _edit_item_message_if_changed(message, text: str, reply_markup) -> None:
    """Idempotently switch between compact text projections and their matching keyboards."""
    if message is None or not hasattr(message, "edit_text"):
        return
    if (
        getattr(message, "text", None) == text
        and getattr(message, "reply_markup", None) == reply_markup
    ):
        return
    if getattr(message, "text", None) == text:
        await _edit_reply_markup_if_changed(message, reply_markup)
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise


async def on_feedback_callback(
    callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Translate feedback callbacks into scoped service calls and UI projections."""
    if not settings.is_allowed(callback.from_user.id) or not callback.data:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) < 3 or parts[0] != "feedback":
        await callback.answer("Некорректное действие")
        return
    action, raw_item_id = parts[1], parts[2]
    try:
        item_id = int(raw_item_id)
    except ValueError:
        await callback.answer("Некорректное сохранение")
        return
    if item_id < 1:
        await callback.answer("Некорректное сохранение")
        return

    if action in {"menu", "category_menu", "type_menu", "back"}:
        if action == "category_menu" and len(parts) == 5 and parts[3] == "page":
            category_page_number = _page_callback_value(parts[4])
        elif action == "category_menu" and len(parts) == 3:
            category_page_number = 0
        else:
            category_page_number = None
        if (action == "category_menu" and category_page_number is None) or (
            action != "category_menu" and len(parts) != 3
        ):
            await callback.answer("Некорректное действие")
            return
        projection = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if projection is None:
            await callback.answer("Сохранение недоступно")
            return
        item, sources = projection
        if action == "menu":
            markup = feedback_menu_keyboard(item_id)
        elif action == "type_menu":
            markup = feedback_type_keyboard(item_id)
        elif action == "category_menu":
            async with session_factory() as session:
                categories = await list_categories_page(
                    session,
                    item.user_id,
                    page=category_page_number,
                    by_frequency=True,
                )
            markup = feedback_category_keyboard(
                item_id,
                [category for category, _count in categories.categories],
                page=categories.page,
                has_previous=categories.has_previous,
                has_next=categories.has_next,
            )
        else:
            markup = item_more_keyboard(item)
        await _edit_reply_markup_if_changed(callback.message, markup)
        await callback.answer()
        return

    idempotency_key = f"telegram-callback:{callback.id}"
    # ❌ Удален handler-level receipt claim в отдельной транзакции: service
    # теперь связывает eligibility/token resolution и receipt в одной записи.
    if action in {
        "useful",
        "not_interesting",
        "priority_higher",
        "priority_lower",
        "summary_wrong",
    }:
        if len(parts) != 3:
            await callback.answer("Некорректное действие")
            return
        event_types = {
            "useful": "USEFUL",
            "not_interesting": "NOT_INTERESTING",
            "priority_higher": "PRIORITY_HIGHER",
            "priority_lower": "PRIORITY_LOWER",
            "summary_wrong": "SUMMARY_REPORTED_WRONG",
        }
        item = await record_item_feedback(
            session_factory,
            callback.from_user.id,
            item_id,
            event_types[action],
            idempotency_key=idempotency_key,
        )
        if item is None:
            await callback.answer("Сохранение недоступно")
            return
        if action in {"useful", "not_interesting"}:
            confirmations = {
                "useful": "Записал 👍",
                "not_interesting": "Записал — буду учитывать",
            }
            await callback.answer(confirmations[action])
            return
        projection = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if projection is not None:
            current_item, _sources = projection
            await _edit_reply_markup_if_changed(
                callback.message, feedback_menu_keyboard(current_item.id)
            )
        confirmations = {
            "priority_higher": "Учту пожелание",
            "priority_lower": "Учту пожелание",
            "summary_wrong": "Отметил сводку как неточную",
        }
        await callback.answer(confirmations[action])
        return

    if action == "category":
        if (
            len(parts) != 4
            or len(parts[3]) != 20
            or any(character not in "0123456789abcdef" for character in parts[3])
        ):
            await callback.answer("Некорректная категория")
            return
        try:
            result = await correct_item_category_by_token(
                session_factory,
                callback.from_user.id,
                item_id,
                parts[3],
                idempotency_key=idempotency_key,
            )
        except ValueError:
            await callback.answer("Категория не подходит")
            return
        if result is None:
            current = await _load_ready_feedback_projection(
                session_factory, callback.from_user.id, item_id
            )
            await callback.answer(
                "Категория больше недоступна" if current is not None else "Сохранение недоступно"
            )
            return
        _updated_item, changed = result
        current = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if current is not None:
            current_item, _sources = current
            await _edit_reply_markup_if_changed(
                callback.message, feedback_menu_keyboard(current_item.id)
            )
        await callback.answer("Категория изменена" if changed else "Категория без изменений")
        return

    if action == "type":
        if len(parts) != 4:
            await callback.answer("Некорректный тип")
            return
        try:
            item_type = ItemType(parts[3])
        except ValueError:
            await callback.answer("Некорректный тип")
            return
        try:
            result = await correct_item_type(
                session_factory,
                callback.from_user.id,
                item_id,
                item_type,
                idempotency_key=idempotency_key,
            )
        except ValueError:
            await callback.answer("Некорректный тип")
            return
        if result is None:
            await callback.answer("Сохранение недоступно")
            return
        _updated_item, changed = result
        current = await _load_ready_feedback_projection(
            session_factory, callback.from_user.id, item_id
        )
        if current is not None:
            current_item, _sources = current
            await _edit_reply_markup_if_changed(
                callback.message, feedback_menu_keyboard(current_item.id)
            )
        await callback.answer("Тип изменён" if changed else "Тип без изменений")
        return

    await callback.answer("Неизвестное действие")


# Callback text is derived from canonical persisted state, not requested input:
# a losing concurrent CAS must never acknowledge an action that did not happen.
def _item_action_label(item: Item, action_name: str) -> str:
    if action_name == "retry":
        return {
            ProcessingStatus.QUEUED: "Повторно поставил в обработку 🔁",
            ProcessingStatus.PROCESSING: "Уже обрабатывается ⏳",
            ProcessingStatus.READY: "Уже обработано ✅",
            ProcessingStatus.FAILED: "Повтор не запущен",
        }[item.processing_status]
    expected_state = {
        "done": ItemState.DONE,
        "archive": ItemState.ARCHIVED,
        "snooze": ItemState.SNOOZED,
        "cancel_snooze": ItemState.ACTIVE,
    }.get(action_name)
    if item.state is expected_state:
        return {
            "done": "Сделано ✅",
            "archive": "В архиве 🗄",
            "snooze": "Отложено ⏰",
            "cancel_snooze": "Отложенное действие отменено",
        }[action_name]
    return {
        ItemState.ACTIVE: "Активно",
        ItemState.SNOOZED: "Отложено ⏰",
        ItemState.DONE: "Сделано ✅",
        ItemState.ARCHIVED: "В архиве 🗄",
    }[item.state]


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


async def on_settings_open_callback(
    callback: CallbackQuery, settings: Settings, session_factory
) -> None:
    """Return from PM-08 controls to the shared canonical settings projection."""
    if not settings.is_allowed(callback.from_user.id) or callback.message is None:
        await callback.answer()
        return
    projection = await _build_settings_projection(
        callback.from_user.id,
        callback.message.chat.id,
        settings,
        session_factory,
    )
    if projection is not None:
        await _edit_item_message_if_changed(callback.message, projection[0], projection[1])
    await callback.answer()


async def on_attention_settings_callback(
    callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker
) -> None:
    """Persist one allowlisted PM-08 choice and redraw only when it changed."""
    if not settings.is_allowed(callback.from_user.id) or not callback.data:
        await callback.answer()
        return
    if callback.data == "settings:attention:status":
        if callback.message is not None:
            await _send_attention_status_for_actor(
                telegram_user_id=callback.from_user.id,
                settings=settings,
                session_factory=session_factory,
                default_timezone=settings.default_timezone,
                send=callback.message.edit_text,
                back_callback="settings:attention:open",
                refresh_callback="settings:attention:status",
            )
        await callback.answer()
        return
    if callback.data == "settings:attention:open":
        if callback.message is not None:
            projection = await _build_settings_projection(
                callback.from_user.id,
                callback.message.chat.id,
                settings,
                session_factory,
                attention=True,
            )
            if projection is not None:
                await _edit_item_message_if_changed(callback.message, projection[0], projection[1])
        await callback.answer()
        return
    current = await get_notification_settings(session_factory, callback.from_user.id)
    if current is None:
        await callback.answer("Пользователь не найден")
        return
    _, values = current
    parts = callback.data.split(":")
    if parts == ["settings", "attention", "toggle"]:
        update = {"attention_enabled": values["attention_enabled"] is not True}
    elif parts == ["settings", "attention", "motivation"]:
        update = {"generic_motivation_enabled": values["generic_motivation_enabled"] is not True}
    elif len(parts) == 4 and parts[:3] == ["settings", "attention", "level"]:
        try:
            level = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный уровень")
            return
        if level == values["attention_intensity"]:
            await callback.answer("Уже выбран этот уровень")
            return
        update = {"attention_intensity": level}
    else:
        await callback.answer("Неизвестная настройка")
        return

    try:
        updated = await update_notification_settings(
            session_factory, callback.from_user.id, **update
        )
    except ValueError as exc:
        await callback.answer(str(exc))
        return
    if updated is None:
        await callback.answer("Пользователь не найден")
        return
    if callback.message:
        _, updated_values = updated
        await callback.message.edit_text(
            format_attention_settings(updated_values),
            reply_markup=attention_settings_keyboard(
                updated_values["attention_enabled"],
                updated_values["attention_intensity"],
                updated_values["generic_motivation_enabled"],
            ),
        )
    await callback.answer()


async def _send_attention_status_for_actor(
    *,
    telegram_user_id: int,
    settings: Settings,
    session_factory,
    default_timezone: str,
    send,
    back_callback: str,
    refresh_callback: str = "nav:attention:status",
) -> None:
    """Present scheduler-owned read-only eligibility without creating Reminder/Event rows."""
    if not settings.is_allowed(telegram_user_id):
        return
    status = await get_attention_status(
        session_factory,
        telegram_user_id,
        default_timezone=default_timezone,
    )
    if status is None:
        await send("Настройки уведомлений не найдены.", reply_markup=None)
        return
    await send(
        format_attention_status(status),
        reply_markup=attention_status_keyboard(back_callback, refresh_callback),
    )


async def on_profile(message: Message, settings: Settings, session_factory) -> None:
    if not settings.is_allowed(message.from_user.id if message.from_user else None):
        return
    await _send_profile_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def _send_profile_for_actor(
    *, telegram_user_id: int, chat_id: int, settings: Settings, session_factory, send
) -> None:
    """Share the existing profile projection while keeping callback actor identity explicit."""
    from app.bot.formatting import format_profile
    from app.services.ingestion import get_or_create_user
    from app.services.profile import get_profile

    async with session_factory() as session:
        # get_or_create + get_profile: lazy seed работает и для первого /profile
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        await session.commit()
        profile = await get_profile(session, user.id)
    await send(format_profile(profile), reply_markup=profile_keyboard())


async def on_profile_update(
    message: Message,
    settings: Settings,
    session_factory,
    instruction: str,
    state: FSMContext | None = None,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(user_id):
        return
    if not instruction:
        if state is not None:
            await _begin_guided_input(
                state,
                GuidedInput.profile,
                message.answer,
                "✏️ Что изменить в профиле? Отправь одну инструкцию сообщением.",
            )
        else:
            await message.answer("Использование: /profile_update <что изменить>")
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


# ❌ Удалены независимые command-only тела Today/Attention/Inbox/Weekly/Profile/Search:
# menu callbacks now use the same actor-explicit projections and preserve each operation's effects.
async def on_today(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    await _send_today_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def _send_today_for_actor(
    *, telegram_user_id: int, chat_id: int, settings: Settings, session_factory, send
) -> None:
    """Share Today selection and post-send exposure across command and menu transports."""
    if not settings.is_allowed(telegram_user_id):
        return
    from app.bot.formatting import format_today
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        user_id = user.id
        items = await TodayService().list_items(session, user.id)
        sources_by_item = await load_item_sources_by_item(session, [item.id for item in items])
        response = format_today(items, sources_by_item)
        await session.commit()
    await send(
        response,
        reply_markup=item_list_keyboard(item_navigation_entries(items, sources_by_item)),
    )

    # PM-07 treats this history as exposure, so a failed Telegram send must not
    # suppress these Items in a later attention preview.
    if items:
        async with session_factory() as session:
            await record_item_events(session, user_id, [item.id for item in items], "TODAY_SHOWN")
            await session.commit()


async def on_weekly(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    await _send_weekly_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def _send_weekly_for_actor(
    *, telegram_user_id: int, chat_id: int, settings: Settings, session_factory, send
) -> None:
    """Resolve the same timezone-aware weekly read projection for either Telegram surface."""
    if not settings.is_allowed(telegram_user_id):
        return
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        try:
            zone = parse_timezone(user.timezone)
        except ValueError:
            log.warning("invalid timezone for weekly review user_id=%s; using default", user.id)
            zone = parse_timezone(settings.default_timezone)
        review = await WeeklyReviewService().build(session, user.id, zone=zone)
        recommendation_ids = [entry.item_id for entry in review.recommendations]
        recommendations = (
            list(
                (
                    await session.scalars(
                        select(Item).where(
                            Item.user_id == user.id,
                            Item.id.in_(recommendation_ids),
                        )
                    )
                ).all()
            )
            if recommendation_ids
            else []
        )
        item_by_id = {item.id: item for item in recommendations}
        ordered_items = [
            item_by_id[item_id] for item_id in recommendation_ids if item_id in item_by_id
        ]
        sources_by_item = await load_item_sources_by_item(
            session, [item.id for item in ordered_items]
        )
        titles_by_id = {
            item.id: item_display_title(item, sources_by_item[item.id]) for item in ordered_items
        }
        response = format_weekly_review(review, titles_by_id)
        await session.commit()
    await send(
        response,
        reply_markup=item_list_keyboard(item_navigation_entries(ordered_items, sources_by_item)),
    )


async def on_attention(
    message: Message, settings: Settings, session_factory, arguments: str = ""
) -> None:
    if not await _allowed(message, settings):
        return
    if not arguments.strip():
        await message.answer(
            "✨ Внимание\n\nСколько показать?",
            reply_markup=attention_chooser_keyboard(),
        )
        return
    await _send_attention_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        arguments=arguments,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def _send_attention_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    arguments: str,
    settings: Settings,
    session_factory,
    send,
) -> None:
    """Keep ranking and per-card post-send exposure identical for command and menu users."""
    if not settings.is_allowed(telegram_user_id):
        return

    parts = arguments.split()
    limit = None
    if parts:
        try:
            if len(parts) != 1:
                raise ValueError
            requested_limit = int(parts[0])
            if not 1 <= requested_limit <= 5:
                raise ValueError
        except ValueError:
            await send("Использование: /attention [1-5]")
            return
        limit = requested_limit

    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        ranked = await AttentionRankingService().list_ranked(session, user.id, limit=limit)
        item_ids = [item.id for item, _ in ranked]
        sources_by_item: dict[int, list[ItemSource]] = {item_id: [] for item_id in item_ids}
        if item_ids:
            sources = (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id.in_(item_ids))
                    .order_by(ItemSource.item_id, ItemSource.source_index, ItemSource.id)
                )
            ).all()
            for source in sources:
                sources_by_item[source.item_id].append(source)
        user_id = user.id
        original_available = user.telegram_chat_id is not None
        await session.commit()

    if not ranked:
        await send("Пока нечего вернуть в фокус.")
        return

    await send("🎯 Сейчас заслуживает внимания:")
    count = len(ranked)
    for index, (item, rank) in enumerate(ranked, start=1):
        await send(
            format_attention_item(index, count, item, rank, sources_by_item[item.id]),
            reply_markup=item_keyboard(
                item,
                sources_by_item[item.id],
                original_available=original_available,
            ),
        )
        # Exposure is durable only after Telegram accepted this card; each Item
        # commits independently so a later delivery failure leaves a truthful prefix.
        async with session_factory() as session:
            await record_item_events(session, user_id, [item.id], "ATTENTION_SHOWN")
            await session.commit()


async def on_inbox(message: Message, settings: Settings, session_factory) -> None:
    if not await _allowed(message, settings):
        return
    await _send_inbox_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def _send_inbox_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    settings: Settings,
    session_factory,
    send,
    page: int = 0,
) -> None:
    """Project one shared, bounded Inbox page for slash and inline entry points."""
    if not settings.is_allowed(telegram_user_id):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        item_page = await list_inbox_page(session, user.id, page=page)
        sources_by_item = await load_item_sources_by_item(
            session, [item.id for item in item_page.items]
        )
        await session.commit()
    entries = item_navigation_entries(item_page.items, sources_by_item)
    keyboard = item_list_keyboard(
        entries,
        previous_callback=(
            f"nav:inbox:page:{item_page.page - 1}" if item_page.has_previous else None
        ),
        next_callback=(f"nav:inbox:page:{item_page.page + 1}" if item_page.has_next else None),
        back_label="← Меню",
    )
    await send(
        format_item_list(list(item_page.items), "📥 Сохранённое", sources_by_item),
        reply_markup=keyboard,
    )


async def on_category(message: Message, settings: Settings, session_factory, category: str) -> None:
    if not await _allowed(message, settings):
        return
    if category:
        await _send_category_items_for_actor(
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            category=category,
            settings=settings,
            session_factory=session_factory,
            send=message.answer,
        )
        return
    await _send_categories_page_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


# ❌ Удален _load_categories_for_actor, который отдавал все категории сразу:
# общий owner-scoped путь теперь загружает только одну ограниченную страницу.
async def _send_categories_page_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    settings: Settings,
    session_factory,
    send,
    page: int = 0,
) -> None:
    """Render owner-scoped category choices through bounded alphabetical pages."""
    if not settings.is_allowed(telegram_user_id):
        return
    from app.bot.formatting import format_categories
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        category_page = await list_categories_page(session, user.id, page=page)
        await session.commit()
    keyboard = category_navigation_keyboard(
        category_page.categories,
        page=category_page.page,
        has_previous=category_page.has_previous,
        has_next=category_page.has_next,
    )
    await send(
        format_categories(category_page.categories, page=category_page.page),
        reply_markup=keyboard,
    )


async def _send_category_items_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    category: str,
    settings: Settings,
    session_factory,
    send,
    page: int = 0,
    category_token_value: str | None = None,
) -> None:
    """Render one priority-ordered category page with its stable callback token."""
    if not settings.is_allowed(telegram_user_id):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        item_page = await list_category_items_page(session, user.id, category, page=page)
        sources_by_item = await load_item_sources_by_item(
            session, [item.id for item in item_page.items]
        )
        await session.commit()
    token = category_token_value or category_token(category)
    entries = item_navigation_entries(item_page.items, sources_by_item)
    keyboard = item_list_keyboard(
        entries,
        previous_callback=(
            f"nav:category:{token}:page:{item_page.page - 1}" if item_page.has_previous else None
        ),
        next_callback=(
            f"nav:category:{token}:page:{item_page.page + 1}" if item_page.has_next else None
        ),
        back_label="← Меню",
    )
    await send(
        format_item_list(
            list(item_page.items),
            f"Категория: {category}",
            sources_by_item,
        ),
        reply_markup=keyboard,
    )


async def on_search(message: Message, settings: Settings, session_factory, query: str) -> None:
    await on_search_with_state(message, settings, session_factory, query, None)


async def on_search_with_state(
    message: Message,
    settings: Settings,
    session_factory,
    query: str,
    state: FSMContext | None,
) -> None:
    """Share one-shot search entry while preserving direct lexical shortcuts."""
    if not await _allowed(message, settings):
        return
    if not query.strip():
        if state is not None:
            await _begin_guided_input(state, GuidedInput.search, message.answer, _SEARCH_PROMPT)
        else:
            await message.answer(_SEARCH_PROMPT)
        return
    await _send_search_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        query=query,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def _send_search_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    query: str,
    settings: Settings,
    session_factory,
    send,
) -> None:
    """Run the existing lexical search and rendering from either input surface."""
    if not settings.is_allowed(telegram_user_id):
        return
    from app.bot.formatting import format_item_list
    from app.services.ingestion import get_or_create_user

    query = query.strip()
    if not query:
        await send(_SEARCH_PROMPT)
        return
    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        items = await search_items(session, user.id, query)
        sources_by_item = await load_item_sources_by_item(session, [item.id for item in items])
        await session.commit()
    if not items:
        await send(
            "Ничего не найдено.\n\n"
            "Поиск совпадает по словам и не понимает синонимы, перевод или транслитерацию.\n"
            "Попробуй другое написание запроса.",
            reply_markup=search_empty_keyboard(),
        )
        return
    await send(
        format_item_list(items, "Результаты поиска:", sources_by_item),
        reply_markup=item_list_keyboard(item_navigation_entries(items, sources_by_item)),
    )


async def on_ask(
    message: Message,
    settings: Settings,
    session_factory,
    question: str,
    state: FSMContext | None = None,
) -> None:
    """Validate and durably enqueue one question; all retrieval and LLM work stays in AskWorker."""
    if not await _allowed(message, settings):
        return
    question = question.strip()
    if not question:
        if state is not None:
            await _begin_guided_input(state, GuidedInput.ask, message.answer, _ASK_PROMPT)
        else:
            await message.answer(_ASK_PROMPT)
        return
    if error := _ask_question_length_error(question):
        await message.answer(error)
        return
    job = await _enqueue_ask_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        question=question,
        settings=settings,
        session_factory=session_factory,
    )
    if job is not None:
        await message.answer("Ищу в сохранённых материалах…")


def _ask_question_length_error(question: str) -> str | None:
    """Apply the same bounded Ask input contract to slash and guided submissions."""
    if len(question) > MAX_ASK_QUESTION_CHARS:
        return f"Вопрос слишком длинный. Максимум {MAX_ASK_QUESTION_CHARS} символов."
    return None


async def _enqueue_ask_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    telegram_message_id: int,
    question: str,
    settings: Settings,
    session_factory,
):
    """Persist one user message as the durable Ask identity; never call the provider here."""
    if not settings.is_allowed(telegram_user_id):
        return None
    job = await enqueue_ask(
        session_factory,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        question=question,
        default_timezone=settings.default_timezone,
    )
    log.info("ask request acknowledged job=%s user_id=%s", job.id, telegram_user_id)
    return job


async def on_export(message: Message, settings: Settings, session_factory) -> None:
    """Translate an authorized command into a durable job; generation remains worker-owned."""
    if not await _allowed(message, settings):
        return
    parts = (message.text or "").split()
    arguments = parts[1:]
    if len(arguments) > 1:
        await message.answer("Использование: /export [compact|full]")
        return
    if not arguments:
        await message.answer("📦 Экспорт", reply_markup=export_chooser_keyboard())
        return
    requested_mode = arguments[0].casefold()
    if requested_mode == "compact":
        mode = COMPACT
    elif requested_mode == "full":
        mode = FULL
    else:
        await message.answer("Использование: /export [compact|full]")
        return

    job = await _enqueue_export_for_actor(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        mode=mode,
        settings=settings,
        session_factory=session_factory,
    )
    if job is not None:
        await message.answer(_export_acknowledgement(job.mode, job.status))


async def _enqueue_export_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    telegram_message_id: int,
    mode: str,
    settings: Settings,
    session_factory,
):
    """Use the existing message-keyed ExportJob boundary for command and menu requests."""
    if not settings.is_allowed(telegram_user_id):
        return None
    job = await enqueue_export(
        session_factory,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        mode=mode,
        default_timezone=settings.default_timezone,
    )
    log.info(
        "export request acknowledged job_id=%s user_id=%s mode=%s",
        job.id,
        telegram_user_id,
        job.mode,
    )
    return job


def _export_acknowledgement(mode: str, status: str) -> str:
    """Render the durable winning mode when callbacks for one chooser arrive more than once."""
    label = "полный" if mode == FULL else "компактный"
    if status in {"PENDING", "RUNNING"}:
        return f"Готовлю {label} экспорт…"
    if status == "DONE":
        return f"Этот {label} экспорт уже подготовлен."
    return f"Не удалось подготовить {label} экспорт. Повтори /export {mode.casefold()}."


async def on_guided_ask_input(
    message: Message,
    settings: Settings,
    session_factory,
    state: FSMContext,
) -> None:
    """Turn one authorized text reply into the existing durable Ask request."""
    telegram_user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(telegram_user_id):
        await state.clear()
        return
    question = (message.text or "").strip()
    if not question:
        return
    if error := _ask_question_length_error(question):
        await message.answer(error)
        return
    job = await _enqueue_ask_for_actor(
        telegram_user_id=telegram_user_id,
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        question=question,
        settings=settings,
        session_factory=session_factory,
    )
    if job is not None:
        await state.clear()
        await message.answer("Ищу в сохранённых материалах…")


async def on_guided_search_input(
    message: Message,
    settings: Settings,
    session_factory,
    state: FSMContext,
) -> None:
    """Consume one guided query through the same FTS presentation path as /search."""
    telegram_user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(telegram_user_id):
        await state.clear()
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Напиши, что найти в сохранённых материалах.")
        return
    # A valid one-shot query is consumed before local search so a failure cannot
    # leave the next ordinary message accidentally routed as another query.
    await state.clear()
    await _send_search_for_actor(
        telegram_user_id=telegram_user_id,
        chat_id=message.chat.id,
        query=query,
        settings=settings,
        session_factory=session_factory,
        send=message.answer,
    )


async def on_guided_profile_input(
    message: Message,
    settings: Settings,
    session_factory,
    state: FSMContext,
) -> None:
    """Route one Profile prompt to the durable update queue, bypassing Item ingestion."""
    telegram_user_id = message.from_user.id if message.from_user else None
    if not settings.is_allowed(telegram_user_id):
        await state.clear()
        return
    instruction = (message.text or "").strip()
    if not instruction:
        await message.answer("Напиши, что изменить в профиле.")
        return
    await enqueue_profile_update(
        session_factory,
        telegram_user_id=telegram_user_id,
        chat_id=message.chat.id,
        instruction=instruction,
        default_timezone=settings.default_timezone,
    )
    await state.clear()
    log.info("guided profile update queued user_id=%s", telegram_user_id)
    await message.answer("Принял. Обновляю профиль…")


def _page_callback_value(value: str) -> int | None:
    """Validate compact page callback input before it reaches SQL OFFSET handling."""
    if not value.isascii() or not value.isdecimal() or len(value) > 18:
        return None
    return int(value)


async def _resolve_category_for_actor(
    *,
    telegram_user_id: int,
    chat_id: int,
    token: str,
    settings: Settings,
    session_factory,
) -> str | None:
    """Resolve a category token against only its current owner in the storage layer."""
    if not settings.is_allowed(telegram_user_id):
        return None
    from app.services.ingestion import get_or_create_user

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=settings.default_timezone,
        )
        category = await resolve_category_token(session, user.id, token)
        await session.commit()
        return category


async def on_navigation_callback(
    callback: CallbackQuery,
    settings: Settings,
    session_factory,
    state: FSMContext,
) -> None:
    """Route inline navigation with the human callback actor, never the bot-authored message."""
    if callback.message is None or callback.data is None:
        await callback.answer()
        return
    telegram_user_id = callback.from_user.id
    if not settings.is_allowed(telegram_user_id):
        if callback.data in {
            "nav:ask",
            "nav:search",
            "nav:profile:edit",
            "nav:input:cancel",
        }:
            await state.clear()
        await callback.answer()
        return

    # Each navigation choice replaces any older one-shot prompt. Guided flows
    # below install their own state only after the prompt UI is successfully shown.
    await state.clear()
    chat_id = callback.message.chat.id
    send = callback.message.answer
    data = callback.data

    async def edit_current_page(text: str, *, reply_markup=None) -> None:
        """Keep pagination on one Telegram message and tolerate stale page callbacks."""
        await _edit_item_message_if_changed(callback.message, text, reply_markup)

    if data == "nav:input:cancel":
        await _edit_item_message_if_changed(callback.message, "Отменено.", None)
        await callback.answer()
    elif data == "nav:menu":
        await _edit_item_message_if_changed(callback.message, "Главное меню:", main_menu_keyboard())
        await callback.answer()
    elif data == "nav:today":
        await callback.answer()
        await _send_today_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=send,
        )
    elif data == "nav:attention":
        # ❌ Удалён автоматический показ списка без выбора количества: menu и /attention
        # теперь ведут в один chooser с конечными значениями.
        await _edit_item_message_if_changed(
            callback.message,
            "✨ Внимание\n\nСколько показать?",
            attention_chooser_keyboard(),
        )
        await callback.answer()
    elif data.startswith("nav:attention:show:"):
        value = data.removeprefix("nav:attention:show:")
        if value not in {"1", "3", "5"}:
            await callback.answer("Количество больше недоступно")
            return
        await callback.answer()
        await _send_attention_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            arguments=value,
            settings=settings,
            session_factory=session_factory,
            send=send,
        )
    elif data == "nav:attention:status":
        await _send_attention_status_for_actor(
            telegram_user_id=telegram_user_id,
            settings=settings,
            session_factory=session_factory,
            default_timezone=settings.default_timezone,
            send=edit_current_page,
            back_callback="nav:attention",
            refresh_callback="nav:attention:status",
        )
        await callback.answer()
    elif data == "nav:inbox":
        await callback.answer()
        await _send_inbox_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=send,
        )
    elif data.startswith("nav:inbox:page:"):
        page = _page_callback_value(data.removeprefix("nav:inbox:page:"))
        if page is None:
            await callback.answer("Страница больше недоступна")
            return
        await callback.answer()
        await _send_inbox_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=edit_current_page,
            page=page,
        )
    elif data == "nav:weekly":
        await callback.answer()
        await _send_weekly_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=send,
        )
    elif data == "nav:profile":
        await callback.answer()
        await _send_profile_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=send,
        )
    elif data == "nav:profile:edit":
        await _begin_guided_input(
            state,
            GuidedInput.profile,
            callback.message.edit_text,
            "✏️ Что изменить в профиле? Отправь одну инструкцию сообщением.",
        )
        await callback.answer()
    elif data == "nav:settings":
        projection = await _build_settings_projection(
            telegram_user_id, chat_id, settings, session_factory
        )
        await callback.answer()
        if projection is not None:
            await send(projection[0], reply_markup=projection[1])
    elif data == "nav:categories":
        await callback.answer()
        await _send_categories_page_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=send,
        )
    elif data.startswith("nav:categories:page:"):
        page = _page_callback_value(data.removeprefix("nav:categories:page:"))
        if page is None:
            await callback.answer("Страница больше недоступна")
            return
        await callback.answer()
        await _send_categories_page_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            settings=settings,
            session_factory=session_factory,
            send=edit_current_page,
            page=page,
        )
    elif data.startswith("nav:category:"):
        parts = data.split(":")
        if len(parts) == 3:
            token, page = parts[2], 0
        elif len(parts) == 5 and parts[3] == "page":
            token = parts[2]
            page = _page_callback_value(parts[4])
        else:
            await callback.answer("Категория больше недоступна")
            return
        if page is None:
            await callback.answer("Страница больше недоступна")
            return
        category = await _resolve_category_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            token=token,
            settings=settings,
            session_factory=session_factory,
        )
        if category is None:
            await callback.answer("Категория больше недоступна")
            return
        await callback.answer()
        await _send_category_items_for_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            category=category,
            settings=settings,
            session_factory=session_factory,
            send=edit_current_page,
            page=page,
            category_token_value=token,
        )
    elif data == "nav:search":
        await _begin_guided_input(
            state, GuidedInput.search, callback.message.edit_text, _SEARCH_PROMPT
        )
        await callback.answer()
    elif data == "nav:ask":
        await _begin_guided_input(state, GuidedInput.ask, callback.message.edit_text, _ASK_PROMPT)
        await callback.answer()
    elif data == "nav:export":
        # ❌ Удалён немедленный Compact export из главного меню: пользовательский выбор
        # формата снова доступен перед созданием durable ExportJob.
        await _edit_item_message_if_changed(
            callback.message,
            "📦 Экспорт",
            export_chooser_keyboard(),
        )
        await callback.answer()
    else:
        await callback.answer("Действие больше недоступно")


async def on_export_mode_callback(
    callback: CallbackQuery, settings: Settings, session_factory
) -> None:
    """Bind both chooser modes to the chooser message's existing durable identity."""
    if callback.message is None or callback.data is None:
        await callback.answer()
        return
    telegram_user_id = callback.from_user.id
    if not settings.is_allowed(telegram_user_id):
        await callback.answer()
        return
    mode = callback.data.removeprefix("export:mode:")
    if mode not in {COMPACT, FULL}:
        await callback.answer("Формат экспорта больше недоступен")
        return
    job = await _enqueue_export_for_actor(
        telegram_user_id=telegram_user_id,
        chat_id=callback.message.chat.id,
        telegram_message_id=callback.message.message_id,
        mode=mode,
        settings=settings,
        session_factory=session_factory,
    )
    if job is None:
        await callback.answer()
        return
    # The first successful insert wins if Compact and Full callbacks race on
    # this one chooser message; report the stored mode and remove both controls.
    await _edit_item_message_if_changed(
        callback.message, _export_acknowledgement(job.mode, job.status), None
    )
    await callback.answer()
