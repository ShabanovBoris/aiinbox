from datetime import UTC, datetime

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from app.bot.handlers import (
    _item_action_label,
    on_attention_settings_callback,
    on_category,
    on_help,
    on_inbox,
    on_search,
    on_settings,
    on_start,
    on_text,
    on_today,
)
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.notifications import get_notification_settings
from app.storage.models import Event, Item, User


def make_message(user_id: int, message_id: int = 1, text: str = "hello") -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


def capture_answers(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return sent


async def test_authorized_text_creates_item_and_replies(settings, session_factory, monkeypatch):
    sent = capture_answers(monkeypatch)
    await on_text(make_message(42), settings, session_factory)
    assert sent == ["Принял. Разбираю…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item is not None
        assert item.processing_status is ProcessingStatus.QUEUED
        assert item.user_note == "hello"


async def test_new_user_gets_configured_default_timezone(settings, session_factory, monkeypatch):
    settings.default_timezone = "Europe/Moscow"
    capture_answers(monkeypatch)
    await on_text(make_message(42), settings, session_factory)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user.timezone == "Europe/Moscow"


async def test_unauthorized_user_is_ignored(settings, session_factory, monkeypatch):
    sent = capture_answers(monkeypatch)
    await on_text(make_message(999), settings, session_factory)
    assert sent == []
    async with session_factory() as session:
        assert await session.scalar(select(Item)) is None
        assert await session.scalar(select(User)) is None


async def test_start_replies_for_authorized_user(settings, monkeypatch):
    sent = capture_answers(monkeypatch)
    await on_start(make_message(42), settings)
    assert len(sent) == 1


async def test_start_silent_for_unauthorized_user(settings, monkeypatch):
    sent = capture_answers(monkeypatch)
    await on_start(make_message(999), settings)
    assert sent == []


async def test_help_lists_mvp_commands(settings, monkeypatch):
    sent = capture_answers(monkeypatch)
    await on_help(make_message(42), settings)
    assert "/today" in sent[0]
    assert "/attention [1-5]" in sent[0]
    assert "/settings" in sent[0]
    assert "/help" in sent[0]
    assert "YouTube-ссылку" in sent[0]
    assert "или видео" not in sent[0]
    assert "/inbox — последние Items" in sent[0]


async def test_two_allowed_users_ingest_separately(settings, session_factory, monkeypatch):
    capture_answers(monkeypatch)
    await on_text(make_message(42, message_id=1, text="a"), settings, session_factory)
    await on_text(make_message(1000, message_id=1, text="b"), settings, session_factory)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 2
        users = (await session.scalars(select(User.telegram_user_id))).all()
        assert sorted(users) == [42, 1000]


async def test_no_success_ack_when_persistence_fails(settings, session_factory, monkeypatch):
    # Регрессия порядка persist → ACK: ошибка БД не должна сопровождаться
    # сообщением об успешном приёме.
    sent = capture_answers(monkeypatch)

    async def failing_ingest(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.bot.handlers.ingest_message", failing_ingest)
    with pytest.raises(RuntimeError):
        await on_text(make_message(42), settings, session_factory)
    assert sent == []


# Pin the delivery boundary that /attention consumes as recent-exposure history.
async def test_today_records_shown_event_after_successful_send(
    settings, session_factory, monkeypatch
):
    sent = []
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="today",
            item_type=ItemType.ACTION,
            title="Do it",
            priority_score=80,
        )
        session.add(item)
        await session.flush()
        item_id = item.id
        await session.commit()

    async def answer_after_send(self, text, **kwargs):
        async with session_factory() as session:
            assert (
                await session.scalar(select(Event.id).where(Event.event_type == "TODAY_SHOWN"))
                is None
            )
        sent.append((text, kwargs))

    monkeypatch.setattr(Message, "answer", answer_after_send)
    await on_today(make_message(42), settings, session_factory)
    assert sent[0][0] == "Сегодня:\n1. Do it — 80/100"
    buttons = [button for row in sent[0][1]["reply_markup"].inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("1", f"item:view:{item_id}")
    ]
    async with session_factory() as session:
        assert (
            await session.scalar(select(Event.event_type).where(Event.event_type == "TODAY_SHOWN"))
            == "TODAY_SHOWN"
        )


# Failed transport must leave no persisted exposure for a later ranking request.
async def test_today_send_failure_does_not_record_shown_event(
    settings, session_factory, monkeypatch
):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        session.add(
            Item(
                user_id=user.id,
                processing_status=ProcessingStatus.READY,
                state=ItemState.ACTIVE,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="today",
                item_type=ItemType.ACTION,
                title="Do it",
                priority_score=80,
            )
        )
        await session.commit()

    async def fail_answer(self, text, **kwargs):
        raise RuntimeError("Telegram send failed")

    monkeypatch.setattr(Message, "answer", fail_answer)
    with pytest.raises(RuntimeError, match="Telegram send failed"):
        await on_today(make_message(42), settings, session_factory)

    async with session_factory() as session:
        assert (
            await session.scalar(select(Event.id).where(Event.event_type == "TODAY_SHOWN")) is None
        )


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (on_inbox, ()),
        (on_category, ("",)),
        (on_search, ("missing",)),
    ],
)
async def test_first_read_only_command_persists_user(
    settings, session_factory, monkeypatch, handler, args
):
    capture_answers(monkeypatch)

    await handler(make_message(42), settings, session_factory, *args)

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user is not None
        assert user.telegram_chat_id == 42


async def test_inbox_category_and_search_selectors_match_visible_list_order(
    settings, session_factory, monkeypatch
):
    from app.services.retrieval import list_category_items, list_inbox, search_items

    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        items = [
            Item(
                user_id=user.id,
                telegram_message_id=index,
                source_index=0,
                processing_status=ProcessingStatus.READY,
                state=ItemState.ACTIVE,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="needle",
                title=title,
                category="Shared",
                item_type=ItemType.ACTION,
                priority_score=priority,
            )
            for index, title, priority in ((1001, "Needle older", 40), (1002, "Needle newer", 80))
        ]
        session.add_all(items)
        await session.commit()
        for item, created_at in zip(
            items, (datetime(2026, 9, 24), datetime(2026, 9, 25)), strict=True
        ):
            item.created_at = created_at
        await session.commit()
        expected = {
            "Входящие:": await list_inbox(session, user.id),
            "Категория: Shared": await list_category_items(session, user.id, "Shared"),
            "Результаты поиска:": await search_items(session, user.id, "needle"),
        }

    sent = []

    async def capture(self, text, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(Message, "answer", capture)
    await on_inbox(make_message(42), settings, session_factory)
    await on_category(make_message(42), settings, session_factory, "Shared")
    await on_search(make_message(42), settings, session_factory, "needle")

    for text, markup in sent:
        heading = next(key for key in expected if text.startswith(key))
        visible_items = expected[heading]
        assert [
            line.split(". ", 1)[1].split(" — ", 1)[0] for line in text.splitlines() if ". " in line
        ] == [item.title for item in visible_items]
        buttons = [button for row in markup.inline_keyboard for button in row]
        assert [button.callback_data for button in buttons] == [
            f"item:view:{item.id}" for item in visible_items
        ]
        assert [button.text for button in buttons] == [
            str(index) for index in range(1, len(visible_items) + 1)
        ]


async def test_settings_command_persists_minimal_notification_settings(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    await on_settings(make_message(42), settings, session_factory, "timezone Europe/Moscow")
    await on_settings(make_message(42), settings, session_factory, "time 08:30")
    assert "Europe/Moscow" in sent[0]
    assert "08:30" in sent[1]
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user.timezone == "Europe/Moscow"
        assert user.settings_json["daily_digest_time"] == "08:30"


async def test_settings_attention_command_and_callbacks_preserve_current_choice(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    await on_settings(make_message(42), settings, session_factory, "attention")
    assert "Attention Manager" in sent[0]
    assert "Normal" in sent[0]
    assert "Generic motivation: ON" in sent[0]

    answered = []
    edited = []

    async def answer_callback(self, text=None, **kwargs):
        answered.append(text)

    async def edit_text(self, text, **kwargs):
        edited.append((text, kwargs))

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_text", edit_text)
    user = TgUser(id=42, is_bot=False, first_name="Test")

    same_level = CallbackQuery(
        id="same-level",
        from_user=user,
        chat_instance="private",
        message=make_message(42),
        data="settings:attention:level:3",
    )
    await on_attention_settings_callback(same_level, settings, session_factory)
    assert edited == []
    assert answered == ["Уже выбран этот уровень"]

    level_change = CallbackQuery(
        id="level-change",
        from_user=user,
        chat_instance="private",
        message=make_message(42),
        data="settings:attention:level:5",
    )
    await on_attention_settings_callback(level_change, settings, session_factory)
    assert "Aggressive" in edited[-1][0]
    updated = await get_notification_settings(session_factory, 42)
    assert updated[1]["attention_intensity"] == 5

    toggle = CallbackQuery(
        id="toggle",
        from_user=user,
        chat_instance="private",
        message=make_message(42),
        data="settings:attention:toggle",
    )
    await on_attention_settings_callback(toggle, settings, session_factory)
    assert "Статус: OFF" in edited[-1][0]
    updated = await get_notification_settings(session_factory, 42)
    assert updated[1]["attention_enabled"] is False

    motivation_toggle = CallbackQuery(
        id="motivation-toggle",
        from_user=user,
        chat_instance="private",
        message=make_message(42),
        data="settings:attention:motivation",
    )
    await on_attention_settings_callback(motivation_toggle, settings, session_factory)
    assert "Generic motivation: OFF" in edited[-1][0]
    updated = await get_notification_settings(session_factory, 42)
    assert updated[1]["attention_enabled"] is False
    assert updated[1]["generic_motivation_enabled"] is False
    markup = edited[-1][1]["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(
        button.callback_data == "settings:attention:motivation"
        and button.text == "💬 Motivation ON"
        for button in buttons
    )

    await on_attention_settings_callback(motivation_toggle, settings, session_factory)
    updated = await get_notification_settings(session_factory, 42)
    assert updated[1]["generic_motivation_enabled"] is True
    assert updated[1]["attention_enabled"] is False


def test_production_router_composition_builds(settings, session_factory):
    # Регрессия: production-вызов make_router (как в app/main.py) собирается
    # без TypeError — router включает profile-команды.
    from aiogram import Router

    from app.bot.handlers import make_router

    router = make_router(settings, session_factory, settings.max_audio_bytes)
    assert isinstance(router, Router)
    names = [h.callback.__name__ for h in router.message.handlers]
    assert "profile" in names and "profile_update" in names and "settings_command" in names
    assert {"help_command", "today", "attention", "inbox", "category", "search"} <= set(names)
    assert "item_action" in [h.callback.__name__ for h in router.callback_query.handlers]
    assert "settings_attention" in [h.callback.__name__ for h in router.callback_query.handlers]


def test_item_action_label_reports_persisted_winner_not_requested_action():
    item = Item(
        user_id=1,
        processing_status=ProcessingStatus.FAILED,
        state=ItemState.DONE,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="x",
    )
    assert _item_action_label(item, "archive") == "Готово ✅"
