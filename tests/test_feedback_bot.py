import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select

from app.bot.formatting import format_ready_item
from app.bot.handlers import on_feedback_callback, on_item_callback, on_reminder_callback
from app.bot.keyboards import (
    category_callback_token,
    feedback_category_keyboard,
    feedback_menu_keyboard,
    item_keyboard,
    item_navigation_keyboard,
    item_sources_keyboard,
    proactive_reminder_keyboard,
)
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services import feedback as feedback_service
from app.services.feedback import correct_item_category
from app.services.retrieval import TodayService
from app.storage.models import (
    Event,
    FeedbackCallbackReceipt,
    Item,
    ItemSource,
    Reminder,
    User,
)


class FakeCallbackMessage:
    """Capture markup-only and canonical correction edits at the Telegram edge."""

    def __init__(self, text=None, reply_markup=None, *, keep_stale_markup=False):
        self.text = text
        self.reply_markup = reply_markup
        self.remote_reply_markup = reply_markup
        self.keep_stale_markup = keep_stale_markup
        self.markup_edits = 0
        self.text_edits = 0
        self.chat = SimpleNamespace(id=42)
        self.sent_answers = []

    async def answer(self, text=None, **kwargs):
        self.sent_answers.append((text, kwargs))

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits += 1
        if self.remote_reply_markup == reply_markup:
            raise TelegramBadRequest(
                method=None,
                message="Bad Request: message is not modified",
            )
        self.remote_reply_markup = reply_markup
        if not self.keep_stale_markup:
            self.reply_markup = reply_markup

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup
        self.remote_reply_markup = reply_markup
        self.text_edits += 1


class FakeCallback:
    """Minimal callback identity lets duplicate transport delivery be replayed."""

    def __init__(self, user_id: int, data: str, callback_id: str = "callback-1"):
        self.id = callback_id
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = FakeCallbackMessage()
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


class FakeCopyBot:
    """Capture Bot API copy attempts so ownership and failure edges stay testable."""

    def __init__(self, error=None):
        self.error = error
        self.copies = []

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)
        if self.error is not None:
            raise self.error


async def _create_ready_item(
    session_factory,
    *,
    category="Programming",
    item_type=ItemType.REFERENCE,
    state=ItemState.ACTIVE,
    interest_level=2,
    priority_score=72,
    completeness=None,
    telegram_message_id=None,
):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            telegram_message_id=telegram_message_id,
            source_index=0,
            processing_status=ProcessingStatus.READY,
            state=state,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            analysis_completeness=completeness,
            user_note="",
            title="Feedback item",
            summary="The original summary",
            category=category,
            item_type=item_type,
            priority_score=priority_score,
            interest_level=interest_level,
        )
        session.add(item)
        await session.commit()
        return item.id


async def _create_category_item(session_factory, category: str) -> int:
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        if user is None:
            user = User(telegram_user_id=42, telegram_chat_id=42)
            session.add(user)
            await session.flush()
        item = Item(
            user_id=user.id,
            telegram_message_id=None,
            source_index=1,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="",
            title=f"Existing {category} category",
            category=category,
            item_type=ItemType.LEARN,
            priority_score=50,
        )
        session.add(item)
        await session.commit()
        return item.id


async def _event_count(session_factory, item_id: int) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count(Event.id)).where(Event.item_id == item_id))


async def _reminder_event_count(session_factory, reminder_id: int, event_type: str) -> int:
    """Count reminder-attributed events without conflating them with Item history."""
    async with session_factory() as session:
        return await session.scalar(
            select(func.count(Event.id)).where(
                Event.reminder_id == reminder_id,
                Event.event_type == event_type,
            )
        )


async def _create_sent_proactive_reminder(session_factory, item_id: int) -> int:
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        reminder = Reminder(
            user_id=user.id,
            item_id=item_id,
            type="PROACTIVE_ATTENTION",
            status="SENT",
            scheduled_at=datetime(2026, 9, 20),
            sent_at=datetime(2026, 9, 20),
            payload_json={"focus_source_id": None},
        )
        session.add(reminder)
        await session.commit()
        return reminder.id


def _callback_data(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def test_ready_primary_keyboard_keeps_only_bounded_content_actions_and_more():
    item = Item(
        id=7,
        user_id=1,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="",
        title="x",
        category="Programming",
        item_type=ItemType.LEARN,
        priority_score=55,
        interest_level=2,
        source_url=None,
        source_metadata_json={
            "forwarded": True,
            "forward_origin_type": "channel",
            "forward_source_username": "public_channel",
            "forward_message_id": 42,
        },
        analysis_completeness="PARTIAL",
    )
    sources = [
        ItemSource(
            id=11,
            item_id=7,
            source_index=0,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=ready",
            extraction_status="READY",
        ),
        ItemSource(
            id=12,
            item_id=7,
            source_index=1,
            source_type=SourceType.WEB,
            source_url="https://example.com/article",
            extraction_status="READY",
        ),
        ItemSource(
            id=13,
            item_id=7,
            source_index=2,
            source_type=SourceType.WEB,
            source_url="https://example.com/failed",
            extraction_status="FAILED",
            metadata_json={"failure_permanent": False},
        ),
    ]

    markup = item_keyboard(item, sources)
    callbacks = _callback_data(markup)
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert "••• Ещё" in labels
    assert not {"1", "2 ✓", "3", "👍 Полезно", "👎 Не моё", "⚙ Исправить"} & set(labels)
    assert not {"item:done:7", "item:later:7", "item:archive:7"} & set(callbacks)
    assert "item:video:7:11" in callbacks
    assert "item:original:7" not in callbacks
    assert "item:sources:7" in callbacks
    assert "item:retry:7" in callbacks
    assert "https://t.me/public_channel/42" in [
        button.url
        for row in item_sources_keyboard(item, sources).inline_keyboard
        for button in row
        if button.url
    ]
    assert not any(label.startswith("🔗 Открыть") for label in labels)


def test_original_action_is_item_provenance_and_does_not_consume_source_bound():
    item = Item(
        id=8,
        user_id=1,
        telegram_message_id=987,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.WEB,
        processing_stage="READY",
        user_note="",
        source_metadata_json={
            "forwarded": True,
            "forward_origin_type": "channel",
            "forward_source_username": "public_channel",
            "forward_message_id": 42,
        },
    )
    source = ItemSource(
        id=20,
        item_id=8,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://example.com/article",
        extraction_status="READY",
    )

    markup = item_keyboard(item, [source])
    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for button in buttons if button.callback_data]
    labels = [button.text for button in buttons]

    assert "item:original:8" in callbacks
    assert "↩️ Оригинал" in labels
    assert {button.url for button in buttons if button.url} == {
        "https://example.com/article",
        "https://t.me/public_channel/42",
    }
    source_markup = item_sources_keyboard(item, [source])
    source_buttons = [button for row in source_markup.inline_keyboard for button in row]
    assert {button.url for button in source_buttons if button.url} == {
        "https://example.com/article",
        "https://t.me/public_channel/42",
    }
    assert "item:original:8" not in _callback_data(source_markup)


def test_original_action_is_hidden_without_capture_or_owner_chat():
    item = Item(
        id=8,
        user_id=1,
        telegram_message_id=987,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="",
    )

    assert "item:original:8" not in _callback_data(item_keyboard(item, original_available=False))
    item.telegram_message_id = None
    assert "item:original:8" not in _callback_data(item_keyboard(item))


def test_numbered_item_navigation_preserves_order_and_callback_bounds():
    item_ids = [9, 20, 31, 2**63 - 1]
    markup = item_navigation_keyboard(item_ids)
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert [(button.text, button.callback_data) for button in buttons] == [
        (str(index), f"item:view:{item_id}") for index, item_id in enumerate(item_ids, 1)
    ]
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons)
    assert (
        len(
            [
                button
                for row in item_navigation_keyboard(list(range(1, 30))).inline_keyboard
                for button in row
            ]
        )
        == 20
    )
    high_id = 2**63 - 1
    item = Item(
        id=high_id,
        user_id=1,
        telegram_message_id=987,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="",
    )
    callbacks = _callback_data(item_keyboard(item)) + _callback_data(
        proactive_reminder_keyboard(high_id, item)
    )
    assert f"item:original:{high_id}" in callbacks
    assert f"reminder:original:{high_id}" in callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


async def test_item_view_sends_a_new_owner_scoped_card_without_events(settings, session_factory):
    item_id = await _create_ready_item(session_factory, telegram_message_id=987)
    callback = FakeCallback(42, f"item:view:{item_id}")

    await on_item_callback(callback, settings, session_factory)

    assert callback.message.text_edits == 0
    assert len(callback.message.sent_answers) == 1
    text, kwargs = callback.message.sent_answers[0]
    assert "✓ Сохранено" in text
    assert f"item:original:{item_id}" in _callback_data(kwargs["reply_markup"])
    assert callback.answers == [None]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 0


async def test_original_callback_copies_owned_capture_without_business_writes(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory, telegram_message_id=987)
    callback = FakeCallback(42, f"item:original:{item_id}")
    callback.bot = FakeCopyBot()

    await on_item_callback(callback, settings, session_factory)

    assert callback.bot.copies == [{"chat_id": 42, "from_chat_id": 42, "message_id": 987}]
    assert callback.answers == ["Оригинал отправлен"]
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.telegram_message_id == 987
        assert stored.state is ItemState.ACTIVE
        assert await session.scalar(select(func.count(Event.id))) == 0


async def test_original_callback_rejects_another_owners_item_before_copy(settings, session_factory):
    async with session_factory() as session:
        owner = User(telegram_user_id=1000, telegram_chat_id=1000)
        session.add(owner)
        await session.flush()
        item = Item(
            user_id=owner.id,
            telegram_message_id=4321,
            source_index=0,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="",
            title="Private item",
        )
        session.add(item)
        await session.commit()
        item_id = item.id
    callback = FakeCallback(42, f"item:original:{item_id}")
    callback.bot = FakeCopyBot()

    await on_item_callback(callback, settings, session_factory)

    assert callback.bot.copies == []
    assert callback.answers == ["Item недоступен"]
    assert "4321" not in str(callback.answers)


async def test_unavailable_original_offers_only_persisted_safe_sources(settings, session_factory):
    item_id = await _create_ready_item(session_factory, telegram_message_id=987)
    async with session_factory() as session:
        session.add(
            ItemSource(
                item_id=item_id,
                source_index=0,
                source_type=SourceType.WEB,
                source_url="https://example.com/article",
                extraction_status="READY",
            )
        )
        await session.commit()
    callback = FakeCallback(42, f"item:original:{item_id}")
    callback.bot = FakeCopyBot(
        TelegramBadRequest(method=None, message="Bad Request: message to copy not found")
    )

    await on_item_callback(callback, settings, session_factory)

    assert "Оригинальное сообщение больше недоступно." in callback.message.sent_answers[0][0]
    assert "Можно открыть сохранённый источник:" in callback.message.sent_answers[0][0]
    markup = callback.message.sent_answers[0][1]["reply_markup"]
    assert [button.url for row in markup.inline_keyboard for button in row if button.url] == [
        "https://example.com/article"
    ]
    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.telegram_message_id == 987
        assert stored.processing_status is ProcessingStatus.READY
        assert await session.scalar(select(func.count(Event.id))) == 0


async def test_temporary_original_copy_error_is_not_marked_unavailable(settings, session_factory):
    item_id = await _create_ready_item(session_factory, telegram_message_id=987)
    callback = FakeCallback(42, f"item:original:{item_id}")
    callback.bot = FakeCopyBot(RuntimeError("temporary Telegram outage"))

    with pytest.raises(RuntimeError, match="temporary Telegram outage"):
        await on_item_callback(callback, settings, session_factory)

    assert callback.message.sent_answers == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 0


async def test_proactive_original_event_follows_successful_copy_once(settings, session_factory):
    item_id = await _create_ready_item(session_factory, telegram_message_id=987)
    reminder_id = await _create_sent_proactive_reminder(session_factory, item_id)
    callback = FakeCallback(42, f"reminder:original:{reminder_id}", "original-click")
    callback.bot = FakeCopyBot()

    await on_reminder_callback(callback, settings, session_factory)
    await on_reminder_callback(callback, settings, session_factory)

    assert len(callback.bot.copies) == 2
    assert callback.answers == ["Оригинал отправлен", "Оригинал отправлен"]
    assert await _reminder_event_count(session_factory, reminder_id, "REMINDER_OPENED") == 1


async def test_failed_proactive_original_copy_creates_no_open_event(settings, session_factory):
    item_id = await _create_ready_item(session_factory, telegram_message_id=987)
    reminder_id = await _create_sent_proactive_reminder(session_factory, item_id)
    callback = FakeCallback(42, f"reminder:original:{reminder_id}")
    callback.bot = FakeCopyBot(
        TelegramBadRequest(method=None, message="Bad Request: message to copy not found")
    )

    await on_reminder_callback(callback, settings, session_factory)

    assert callback.answers == ["Оригинал недоступен"]
    assert callback.message.sent_answers[0][0] == "Оригинальное сообщение больше недоступно."
    assert await _reminder_event_count(session_factory, reminder_id, "REMINDER_OPENED") == 0


async def test_proactive_original_cannot_copy_another_users_reminder(settings, session_factory):
    async with session_factory() as session:
        owner = User(telegram_user_id=1000, telegram_chat_id=1000)
        session.add(owner)
        await session.flush()
        item = Item(
            user_id=owner.id,
            telegram_message_id=4321,
            source_index=0,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="",
        )
        session.add(item)
        await session.flush()
        reminder = Reminder(
            user_id=owner.id,
            item_id=item.id,
            type="PROACTIVE_ATTENTION",
            status="SENT",
            scheduled_at=datetime(2026, 9, 20),
            sent_at=datetime(2026, 9, 20),
        )
        session.add(reminder)
        await session.commit()
        reminder_id = reminder.id
    callback = FakeCallback(42, f"reminder:original:{reminder_id}")
    callback.bot = FakeCopyBot()

    await on_reminder_callback(callback, settings, session_factory)

    assert callback.bot.copies == []
    assert callback.answers == ["Это напоминание сейчас недоступно"]
    assert await _reminder_event_count(session_factory, reminder_id, "REMINDER_OPENED") == 0


def test_non_ready_keyboard_does_not_expose_feedback_controls():
    item = Item(
        id=7,
        user_id=1,
        processing_status=ProcessingStatus.PROCESSING,
        state=ItemState.ACTIVE,
        source_type=SourceType.TEXT,
        processing_stage="ANALYZING",
        user_note="",
        title=None,
    )

    callbacks = _callback_data(item_keyboard(item))
    assert not any(value.startswith("feedback:") for value in callbacks)
    assert not any(value.startswith("item:interest:") for value in callbacks)


def test_category_callback_tokens_fit_telegram_limit_for_long_unicode_values():
    category = "Очень длинная категория для проверки Unicode " * 2
    markup = feedback_category_keyboard(9_223_372_036_854_775_807, [category])
    button = markup.inline_keyboard[0][0]

    assert button.callback_data == (
        f"feedback:category:9223372036854775807:{category_callback_token(category)}"
    )
    assert len(button.callback_data.encode("utf-8")) <= 64
    assert len(button.text) <= 64


def test_category_keyboard_bounds_the_number_of_choices():
    markup = feedback_category_keyboard(7, [f"Category {index}" for index in range(30)])

    assert len(markup.inline_keyboard) == 21


async def test_feedback_back_returns_to_more_hierarchy_without_events(settings, session_factory):
    item_id = await _create_ready_item(session_factory, completeness="PARTIAL")
    async with session_factory() as session:
        session.add_all(
            [
                ItemSource(
                    item_id=item_id,
                    source_index=0,
                    source_type=SourceType.YOUTUBE,
                    source_url="https://www.youtube.com/watch?v=ready",
                    extraction_status="READY",
                ),
                ItemSource(
                    item_id=item_id,
                    source_index=1,
                    source_type=SourceType.WEB,
                    source_url="https://example.com/failed",
                    extraction_status="FAILED",
                ),
            ]
        )
        await session.commit()
        item = await session.get(Item, item_id)
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item_id)
                    .order_by(ItemSource.source_index)
                )
            ).all()
        )

    message = FakeCallbackMessage(reply_markup=item_keyboard(item, sources))
    opened = FakeCallback(42, f"feedback:menu:{item_id}", "open-menu")
    opened.message = message
    await on_feedback_callback(opened, settings, session_factory)
    assert f"feedback:priority_higher:{item_id}" in _callback_data(message.reply_markup)
    assert await _event_count(session_factory, item_id) == 0

    category_menu = FakeCallback(42, f"feedback:category_menu:{item_id}", "categories")
    category_menu.message = message
    await on_feedback_callback(category_menu, settings, session_factory)
    assert (
        f"feedback:category:{item_id}:{category_callback_token('Programming')}"
        in _callback_data(message.reply_markup)
    )
    assert await _event_count(session_factory, item_id) == 0

    type_menu = FakeCallback(42, f"feedback:type_menu:{item_id}", "types")
    type_menu.message = message
    await on_feedback_callback(type_menu, settings, session_factory)
    assert {f"feedback:type:{item_id}:{item_type.value}" for item_type in ItemType} <= set(
        _callback_data(message.reply_markup)
    )
    assert await _event_count(session_factory, item_id) == 0

    return_to_menu = FakeCallback(42, f"feedback:menu:{item_id}", "return-to-menu")
    return_to_menu.message = message
    await on_feedback_callback(return_to_menu, settings, session_factory)
    assert f"feedback:priority_higher:{item_id}" in _callback_data(message.reply_markup)

    back = FakeCallback(42, f"feedback:back:{item_id}", "back")
    back.message = message
    await on_feedback_callback(back, settings, session_factory)

    callbacks = _callback_data(message.reply_markup)
    assert f"item:interest_menu:{item_id}" in callbacks
    assert f"feedback:menu:{item_id}" in callbacks
    assert f"item:details:{item_id}" in callbacks
    assert f"item:back:{item_id}" in callbacks
    assert not any(value.startswith("item:video:") for value in callbacks)
    assert await _event_count(session_factory, item_id) == 0


async def test_feedback_callback_duplicate_is_durable_and_preserves_interest(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory, interest_level=3)
    first = FakeCallback(42, f"feedback:not_interesting:{item_id}", "same-click")
    duplicate = FakeCallback(42, f"feedback:not_interesting:{item_id}", "same-click")

    await on_feedback_callback(first, settings, session_factory)
    await on_feedback_callback(duplicate, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert stored.state is ItemState.ACTIVE
        assert stored.interest_level == 3
        assert event.event_type == "NOT_INTERESTING"
        assert event.idempotency_key == "telegram-callback:same-click"
    assert first.answers == ["Записал — буду учитывать"]
    assert duplicate.answers == ["Записал — буду учитывать"]
    assert await _event_count(session_factory, item_id) == 1


async def test_reminder_terminal_callbacks_are_transport_and_markup_idempotent(
    settings, session_factory
):
    done_item_id = await _create_ready_item(session_factory)
    done_reminder_id = await _create_sent_proactive_reminder(session_factory, done_item_id)
    done = FakeCallback(42, f"reminder:done:{done_reminder_id}", "reminder-done-retry")
    done.message = FakeCallbackMessage(reply_markup="old-keyboard", keep_stale_markup=True)

    await on_reminder_callback(done, settings, session_factory)
    await on_reminder_callback(done, settings, session_factory)

    assert done.answers == ["Готово", "Уже учтено"]
    assert done.message.markup_edits == 1
    assert done.message.remote_reply_markup is None

    # A different Telegram callback can reach an already-applied semantic action;
    # the stale local markup then hits Telegram's no-op error and remains benign.
    semantic_retry = FakeCallback(42, f"reminder:done:{done_reminder_id}", "done-new-id")
    semantic_retry.message = done.message
    await on_reminder_callback(semantic_retry, settings, session_factory)
    assert semantic_retry.answers == ["Уже учтено"]
    assert done.message.markup_edits == 2

    dislike_item_id = await _create_category_item(session_factory, "AI")
    dislike_reminder_id = await _create_sent_proactive_reminder(session_factory, dislike_item_id)
    dislike = FakeCallback(42, f"reminder:less:{dislike_reminder_id}", "reminder-dislike-retry")
    dislike.message = FakeCallbackMessage(reply_markup="old-keyboard", keep_stale_markup=True)
    await on_reminder_callback(dislike, settings, session_factory)
    await on_reminder_callback(dislike, settings, session_factory)
    assert dislike.answers == [
        "Записал — буду показывать меньше похожих",
        "Уже учтено",
    ]
    assert dislike.message.markup_edits == 1


async def test_reminder_later_and_cancel_retries_preserve_current_keyboard(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory)
    reminder_id = await _create_sent_proactive_reminder(session_factory, item_id)
    later = FakeCallback(42, f"reminder:later:{reminder_id}", "reminder-later-retry")
    later.message = FakeCallbackMessage(reply_markup="proactive", keep_stale_markup=True)

    await on_reminder_callback(later, settings, session_factory)
    snooze_markup = later.message.remote_reply_markup
    await on_reminder_callback(later, settings, session_factory)

    assert later.answers == ["Выбери срок", "Уже учтено"]
    assert later.message.markup_edits == 1
    assert snooze_markup != "proactive"

    cancel = FakeCallback(42, f"reminder:cancel:{reminder_id}", "reminder-cancel-retry")
    cancel.message = FakeCallbackMessage(reply_markup=snooze_markup, keep_stale_markup=True)
    await on_reminder_callback(cancel, settings, session_factory)
    proactive_markup = cancel.message.remote_reply_markup
    await on_reminder_callback(cancel, settings, session_factory)

    assert cancel.answers == ["Выбор отменён", "Уже учтено"]
    assert cancel.message.markup_edits == 1
    assert proactive_markup != snooze_markup
    callbacks = _callback_data(proactive_markup)
    assert f"reminder:done:{reminder_id}" in callbacks


async def test_primary_feedback_clicks_with_different_ids_remain_distinct(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory, interest_level=1)

    await on_feedback_callback(
        FakeCallback(42, f"feedback:useful:{item_id}", "first"), settings, session_factory
    )
    await on_feedback_callback(
        FakeCallback(42, f"feedback:useful:{item_id}", "later"), settings, session_factory
    )

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        events = list((await session.scalars(select(Event).where(Event.item_id == item_id))).all())
        assert stored.interest_level == 1
        assert [event.event_type for event in events] == ["USEFUL", "USEFUL"]


async def test_category_callback_corrects_item_and_keeps_feedback_menu_open(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory, category="Programming")
    await _create_category_item(session_factory, "AI")
    message = FakeCallbackMessage(
        text="Old result",
        reply_markup=feedback_category_keyboard(item_id, ["Programming", "AI"]),
    )
    callback = FakeCallback(
        42,
        f"feedback:category:{item_id}:{category_callback_token('AI')}",
        "category-correction",
    )
    callback.message = message

    await on_feedback_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert stored.category == "AI"
        assert event.event_type == "CATEGORY_CORRECTED"
        assert event.payload_json["from"] == "Programming"
        assert event.payload_json["to"] == "AI"
    assert message.text == "Old result"
    assert f"feedback:back:{item_id}" in _callback_data(message.reply_markup)
    assert f"feedback:useful:{item_id}" in _callback_data(message.reply_markup)
    assert callback.answers == ["Категория изменена"]


async def test_replayed_category_callback_keeps_feedback_menu_without_reapplying(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory, category="Programming")
    await _create_category_item(session_factory, "AI")
    await _create_category_item(session_factory, "Piano")
    await correct_item_category(
        session_factory,
        42,
        item_id,
        "AI",
        idempotency_key="telegram-callback:replayed-category",
    )
    await correct_item_category(
        session_factory,
        42,
        item_id,
        "Piano",
        idempotency_key="telegram-callback:later-category",
    )
    message = FakeCallbackMessage(
        text="stale result",
        reply_markup=feedback_category_keyboard(item_id, ["AI", "Piano"]),
    )
    callback = FakeCallback(
        42,
        f"feedback:category:{item_id}:{category_callback_token('AI')}",
        "replayed-category",
    )
    callback.message = message

    await on_feedback_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        corrections = list(
            (
                await session.scalars(
                    select(Event).where(
                        Event.item_id == item_id,
                        Event.event_type == "CATEGORY_CORRECTED",
                    )
                )
            ).all()
        )
        assert stored.category == "Piano"
        assert len(corrections) == 2
    assert message.text == "stale result"
    assert f"feedback:back:{item_id}" in _callback_data(message.reply_markup)
    assert callback.answers == ["Категория без изменений"]


async def test_stale_category_callback_cannot_apply_after_category_returns(
    settings, session_factory, monkeypatch
):
    await _create_category_item(session_factory, "Other")
    item_id = await _create_category_item(session_factory, "Programming")
    async with session_factory() as session:
        target = await session.get(Item, item_id)
        assert target.user_id != item_id
    callback = FakeCallback(
        42,
        f"feedback:category:{item_id}:{category_callback_token('AI')}",
        "stale-category",
    )

    receipt_claimed = asyncio.Event()
    release_callback = asyncio.Event()
    original_claim = feedback_service.claim_feedback_callback_receipt
    paused = False

    async def pause_after_claim(session, user_id, idempotency_key):
        nonlocal paused
        claimed = await original_claim(session, user_id, idempotency_key)
        if idempotency_key == "telegram-callback:stale-category" and claimed and not paused:
            paused = True
            receipt_claimed.set()
            await release_callback.wait()
        return claimed

    monkeypatch.setattr(feedback_service, "claim_feedback_callback_receipt", pause_after_claim)
    stale_delivery = asyncio.create_task(on_feedback_callback(callback, settings, session_factory))
    await asyncio.wait_for(receipt_claimed.wait(), timeout=5)
    category_creation = asyncio.create_task(_create_category_item(session_factory, "AI"))
    replay = asyncio.create_task(on_feedback_callback(callback, settings, session_factory))
    await asyncio.sleep(0)
    release_callback.set()
    await asyncio.gather(stale_delivery, category_creation, replay)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        corrections = list(
            (
                await session.scalars(
                    select(Event).where(
                        Event.item_id == item_id,
                        Event.event_type == "CATEGORY_CORRECTED",
                    )
                )
            ).all()
        )
        receipt = await session.scalar(
            select(FeedbackCallbackReceipt).where(
                FeedbackCallbackReceipt.user_id == stored.user_id,
                FeedbackCallbackReceipt.idempotency_key == "telegram-callback:stale-category",
            )
        )
        assert stored.category == "Programming"
        assert corrections == []
        assert receipt is not None
    assert len(callback.answers) == 2
    assert set(callback.answers) == {"Категория больше недоступна", "Категория без изменений"}


async def test_type_callback_updates_today_and_keeps_priority(settings, session_factory):
    item_id = await _create_ready_item(session_factory, item_type=ItemType.REFERENCE)
    message = FakeCallbackMessage(reply_markup=feedback_menu_keyboard(item_id))
    callback = FakeCallback(42, f"feedback:type:{item_id}:LEARN", "type-correction")
    callback.message = message

    await on_feedback_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        today = await TodayService().list_items(session, stored.user_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert stored.item_type is ItemType.LEARN
        assert stored.priority_score == 72
        assert [item.id for item in today] == [item_id]
        assert event.event_type == "TYPE_CORRECTED"
    assert message.text is None
    assert f"feedback:back:{item_id}" in _callback_data(message.reply_markup)
    assert callback.answers == ["Тип изменён"]


async def test_priority_feedback_stays_in_menu_without_changing_score(settings, session_factory):
    item_id = await _create_ready_item(session_factory)
    message = FakeCallbackMessage(reply_markup=feedback_menu_keyboard(item_id))
    callback = FakeCallback(42, f"feedback:priority_higher:{item_id}", "priority-higher")
    callback.message = message

    await on_feedback_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        event = await session.scalar(select(Event).where(Event.item_id == item_id))
        assert stored.priority_score == 72
        assert event.event_type == "PRIORITY_HIGHER"
        assert event.payload_json["priority_score_at_feedback"] == 72
    callbacks = _callback_data(message.reply_markup)
    assert f"feedback:back:{item_id}" in callbacks
    assert f"feedback:priority_higher:{item_id}" in callbacks
    assert callback.answers == ["Записал сигнал о приоритете"]


async def test_malformed_stale_and_unauthorized_feedback_callbacks_do_not_mutate(
    settings, session_factory
):
    item_id = await _create_ready_item(session_factory)
    callbacks = [
        FakeCallback(42, "feedback:type"),
        FakeCallback(42, f"feedback:type:{item_id}:INVALID"),
        FakeCallback(42, f"feedback:category:{item_id}:not-a-token"),
        FakeCallback(42, "feedback:useful:not-an-id"),
        FakeCallback(1000, f"feedback:useful:{item_id}"),
        FakeCallback(42, "feedback:useful:999999"),
    ]

    for callback in callbacks:
        await on_feedback_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.category == "Programming"
        assert stored.item_type is ItemType.REFERENCE
        assert stored.priority_score == 72
    assert await _event_count(session_factory, item_id) == 0


async def test_category_noop_closes_menu_without_editing_summary(settings, session_factory):
    item_id = await _create_ready_item(session_factory, category="Programming")
    message = FakeCallbackMessage(
        text=format_ready_item(
            Item(
                id=item_id,
                user_id=1,
                processing_status=ProcessingStatus.READY,
                state=ItemState.ACTIVE,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="",
                title="Feedback item",
                summary="The original summary",
                category="Programming",
                item_type=ItemType.REFERENCE,
                priority_score=72,
                interest_level=2,
            )
        ),
        reply_markup=feedback_category_keyboard(item_id, ["Programming"]),
    )
    callback = FakeCallback(
        42,
        f"feedback:category:{item_id}:{category_callback_token('Programming')}",
        "category-noop",
    )
    callback.message = message

    await on_feedback_callback(callback, settings, session_factory)

    assert message.text_edits == 0
    assert message.markup_edits == 1
    assert callback.answers == ["Категория без изменений"]
    assert await _event_count(session_factory, item_id) == 0
