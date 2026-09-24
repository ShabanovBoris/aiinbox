from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from app.bot.handlers import (
    _item_action_label,
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

    async def answer_after_send(self, text, **kwargs):
        async with session_factory() as session:
            assert (
                await session.scalar(select(Event.id).where(Event.event_type == "TODAY_SHOWN"))
                is None
            )
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer_after_send)
    await on_today(make_message(42), settings, session_factory)
    assert sent == ["Сегодня:\n1. Do it — 80/100"]
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
