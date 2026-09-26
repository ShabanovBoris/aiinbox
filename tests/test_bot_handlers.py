from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    Update,
)
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from app.bot.handlers import (
    ClearGuidedInputOnCommandMiddleware,
    GuidedInput,
    _item_action_label,
    make_router,
    on_ask,
    on_attention,
    on_attention_settings_callback,
    on_category,
    on_export,
    on_export_mode_callback,
    on_guided_ask_input,
    on_guided_notification_setting_input,
    on_guided_profile_input,
    on_guided_search_input,
    on_help,
    on_inbox,
    on_navigation_callback,
    on_profile,
    on_profile_update,
    on_search,
    on_search_with_state,
    on_settings,
    on_settings_edit_callback,
    on_settings_open_callback,
    on_start,
    on_text,
    on_today,
)
from app.bot.keyboards import (
    help_keyboard,
    main_menu_keyboard,
    profile_keyboard,
    settings_keyboard,
)
from app.bot.navigation import BOT_COMMANDS, configure_bot_commands
from app.domain.category_tokens import category_token
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.ask_inbox import MAX_ASK_QUESTION_CHARS
from app.services.notifications import get_notification_settings
from app.storage.models import AskJob, Content, Event, ExportJob, Item, ProfileUpdateJob, User


def make_message(user_id: int, message_id: int = 1, text: str = "hello") -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


def make_bot_message(message_id: int = 700, chat_id: int = 42, text: str = "Menu") -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=9999, is_bot=True, first_name="AIInbox"),
        text=text,
    )


class FakeFSMContext:
    def __init__(self, state: str | None = None):
        self.value = state

    async def get_state(self):
        return self.value

    async def set_state(self, state):
        self.value = state.state

    async def clear(self):
        self.value = None


def make_callback(user_id: int, data: str, *, message_id: int = 700) -> CallbackQuery:
    return CallbackQuery(
        id=f"callback-{user_id}-{message_id}-{data}",
        from_user=TgUser(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="private",
        message=make_bot_message(message_id=message_id, chat_id=user_id),
        data=data,
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


async def test_help_is_compact_task_oriented_and_has_inline_menu(settings, monkeypatch):
    sent = capture_answers(monkeypatch)
    await on_help(make_message(42), settings)
    assert "Найти" in sent[0]
    assert "Сегодня" in sent[0]
    assert "Настройки" in sent[0]
    assert "Slash-команды тоже работают." in sent[0]
    assert len(sent[0]) < 500


def test_profile_and_help_keyboards_expose_edit_and_explicit_export_modes():
    profile_callbacks = {
        button.callback_data for row in profile_keyboard().inline_keyboard for button in row
    }
    help_callbacks = {
        button.callback_data for row in help_keyboard().inline_keyboard for button in row
    }

    assert "nav:profile:edit" in profile_callbacks
    assert {"export:mode:COMPACT", "export:mode:FULL"} <= help_callbacks
    assert "nav:export" not in help_callbacks


async def test_help_handler_keeps_compact_and_full_export_actions(settings, monkeypatch):
    responses = []

    async def answer_message(self, text, **kwargs):
        responses.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(Message, "answer", answer_message)
    await on_help(make_message(42), settings)

    callbacks = {button.callback_data for row in responses[0][1].inline_keyboard for button in row}
    assert {"export:mode:COMPACT", "export:mode:FULL"} <= callbacks


async def test_profile_command_and_menu_share_the_edit_keyboard(
    settings, session_factory, monkeypatch
):
    sent = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await on_profile(make_message(42), settings, session_factory)
    await on_navigation_callback(
        make_callback(42, "nav:profile"), settings, session_factory, FakeFSMContext()
    )

    assert len(sent) == 2
    for _text, markup in sent:
        assert "nav:profile:edit" in {
            button.callback_data for row in markup.inline_keyboard for button in row
        }


async def test_no_argument_profile_update_uses_guided_one_shot_flow(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    state = FakeFSMContext()
    await on_profile_update(
        make_message(42, text="/profile_update"), settings, session_factory, "", state
    )
    assert state.value == GuidedInput.profile.state
    assert "Что изменить в профиле?" in sent[0]

    await on_guided_profile_input(
        make_message(42, message_id=823, text="Учитывай цель изучать Kotlin"),
        settings,
        session_factory,
        state,
    )
    assert state.value is None
    await on_profile_update(
        make_message(42, message_id=824, text="/profile_update прямое изменение"),
        settings,
        session_factory,
        "прямое изменение",
    )
    async with session_factory() as session:
        jobs = list(
            (await session.scalars(select(ProfileUpdateJob).order_by(ProfileUpdateJob.id))).all()
        )
        assert [job.instruction for job in jobs] == [
            "Учитывай цель изучать Kotlin",
            "прямое изменение",
        ]
        assert await session.scalar(select(func.count()).select_from(Item)) == 0


def test_bot_commands_are_bounded_and_main_menu_is_inline():
    assert [command.command for command in BOT_COMMANDS] == [
        "start",
        "menu",
        "today",
        "attention",
        "inbox",
        "search",
        "ask",
        "weekly",
        "category",
        "profile",
        "settings",
        "export",
        "help",
    ]
    assert all(command.description and len(command.description) <= 256 for command in BOT_COMMANDS)
    keyboard = main_menu_keyboard()
    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert {button.callback_data for row in keyboard.inline_keyboard for button in row} == {
        "nav:today",
        "nav:attention",
        "nav:inbox",
        "nav:search",
        "nav:ask",
        "nav:weekly",
        "nav:categories",
        "nav:profile",
        "nav:settings",
        "nav:export",
    }


async def test_bot_command_setup_failure_is_safe_and_nonfatal(caplog):
    class FailingBot:
        async def set_my_commands(self, commands):
            raise RuntimeError("PRIVATE_PROVIDER_BODY_47")

    with caplog.at_level("WARNING", logger="app.bot.navigation"):
        await configure_bot_commands(FailingBot())
    assert "operation=set_my_commands" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "PRIVATE_PROVIDER_BODY_47" not in caplog.text


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
        ("Do it", f"item:view:{item_id}")
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


async def test_navigation_today_uses_callback_actor_and_records_after_send(
    settings, session_factory, monkeypatch
):
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
        await session.commit()

    sent = []
    callback_answers = []

    async def answer_message(self, text, **kwargs):
        async with session_factory() as session:
            assert (
                await session.scalar(select(Event.id).where(Event.event_type == "TODAY_SHOWN"))
                is None
            )
        sent.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        callback_answers.append(text)

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    callback = make_callback(42, "nav:today")
    assert callback.message.from_user.is_bot is True

    await on_navigation_callback(callback, settings, session_factory, FakeFSMContext("ask"))

    assert callback_answers == [None]
    assert sent[0][0] == "Сегодня:\n1. Do it — 80/100"
    async with session_factory() as session:
        assert (
            await session.scalar(select(Event.event_type).where(Event.event_type == "TODAY_SHOWN"))
            == "TODAY_SHOWN"
        )
        assert await session.scalar(select(User.id).where(User.telegram_user_id == 9999)) is None


@pytest.mark.parametrize("data", ["nav:profile", "nav:settings", "nav:categories", "nav:ask"])
async def test_unauthorized_navigation_returns_no_private_projection(
    settings, session_factory, monkeypatch, data
):
    sent = []
    answers = []

    async def answer_message(self, text, **kwargs):
        sent.append(text)

    async def answer_callback(self, text=None, **kwargs):
        answers.append(text)

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    state = FakeFSMContext(GuidedInput.ask.state)

    await on_navigation_callback(make_callback(999, data), settings, session_factory, state)

    assert sent == []
    assert answers == [None]
    if data == "nav:ask":
        assert state.value is None
    async with session_factory() as session:
        assert await session.scalar(select(User.id)) is None


async def test_guided_ask_uses_question_message_identity_and_is_one_shot(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    state = FakeFSMContext(GuidedInput.ask.state)
    message = make_message(42, message_id=812, text="What did I save about local models?")

    await on_guided_ask_input(message, settings, session_factory, state)

    assert state.value is None
    assert sent == ["Ищу в сохранённых материалах…"]
    async with session_factory() as session:
        jobs = list((await session.scalars(select(AskJob))).all())
        assert len(jobs) == 1
        assert jobs[0].status == "PENDING"
        assert jobs[0].question == "What did I save about local models?"
        assert jobs[0].telegram_message_id == 812
        assert await session.scalar(select(func.count()).select_from(Item)) == 0

    overlong_state = FakeFSMContext(GuidedInput.ask.state)
    await on_guided_ask_input(
        make_message(42, message_id=816, text="x" * (MAX_ASK_QUESTION_CHARS + 1)),
        settings,
        session_factory,
        overlong_state,
    )
    assert overlong_state.value == GuidedInput.ask.state
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AskJob)) == 1


async def test_no_argument_ask_prompt_and_direct_question_keep_same_durable_boundary(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    state = FakeFSMContext()

    await on_ask(
        make_message(42, message_id=819, text="/ask"), settings, session_factory, "", state
    )

    assert state.value == GuidedInput.ask.state
    assert "по найденным сохранённым материалам" in sent[0]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AskJob)) == 0
        assert await session.scalar(select(func.count()).select_from(Item)) == 0

    await on_guided_ask_input(
        make_message(42, message_id=820, text="Что я сохранял про Kotlin?"),
        settings,
        session_factory,
        state,
    )
    assert state.value is None
    await on_ask(
        make_message(42, message_id=821, text="/ask direct question"),
        settings,
        session_factory,
        "direct question",
    )
    async with session_factory() as session:
        jobs = list((await session.scalars(select(AskJob).order_by(AskJob.id))).all())
        assert [job.question for job in jobs] == ["Что я сохранял про Kotlin?", "direct question"]
        assert [job.telegram_message_id for job in jobs] == [820, 821]
        assert await session.scalar(select(func.count()).select_from(Item)) == 0


async def test_guided_search_uses_fts_without_ask_or_item_ingestion(
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
                user_note="",
                item_type=ItemType.LEARN,
                title="Local Android models",
                summary="Notes about on-device inference.",
                priority_score=50,
            )
        )
        await session.commit()

    sent = capture_answers(monkeypatch)
    state = FakeFSMContext(GuidedInput.search.state)
    await on_guided_search_input(
        make_message(42, message_id=813, text="Android"), settings, session_factory, state
    )

    assert state.value is None
    assert "Local Android models" in sent[0]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AskJob)) == 0
        assert await session.scalar(select(func.count()).select_from(Item)) == 1


async def test_no_argument_search_is_guided_and_explains_lexical_miss(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    state = FakeFSMContext()
    await on_search_with_state(
        make_message(42, text="/search"), settings, session_factory, "", state
    )
    assert state.value == GuidedInput.search.state
    assert "Поиск по словам" in sent[0]
    assert "не смысловой" in sent[0]

    await on_guided_search_input(
        make_message(42, message_id=822, text="Андроид"), settings, session_factory, state
    )
    assert state.value is None
    assert "Ничего не найдено." in sent[-1]
    assert "синонимы, перевод или транслитерацию" in sent[-1]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AskJob)) == 0
        assert await session.scalar(select(func.count()).select_from(Item)) == 0


async def test_command_clears_ask_state_and_forwarded_slash_text_stays_ingestion(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    middleware = ClearGuidedInputOnCommandMiddleware()
    ask_state = FakeFSMContext(GuidedInput.ask.state)

    async def command_handler(event, data):
        await on_today(event, settings, session_factory)

    await middleware(command_handler, make_message(42, text="/today"), {"state": ask_state})
    assert ask_state.value is None
    assert sent and "Сегодня" in sent[0]

    forwarded_state = FakeFSMContext(GuidedInput.ask.state)
    forwarded = Message(
        message_id=814,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="Test"),
        text="/today is text from the forwarded author",
        forward_origin=MessageOriginChannel(
            type="channel",
            date=datetime.now(UTC),
            chat=Chat(id=-1001, type="channel", title="Source"),
            message_id=815,
        ),
    )

    async def forwarded_handler(event, data):
        await on_text(event, settings, session_factory)

    await middleware(forwarded_handler, forwarded, {"state": forwarded_state})
    assert forwarded_state.value == GuidedInput.ask.state
    async with session_factory() as session:
        item = await session.scalar(select(Item).where(Item.telegram_message_id == 814))
        assert item is not None
        content = await session.scalar(select(Content).where(Content.item_id == item.id))
        assert content.text == "/today is text from the forwarded author"
        assert await session.scalar(select(func.count()).select_from(AskJob)) == 0


async def test_category_navigation_is_bounded_owner_scoped_and_selectable(
    settings, session_factory, monkeypatch
):
    async with session_factory() as session:
        owner = User(telegram_user_id=42, telegram_chat_id=42)
        other = User(telegram_user_id=1000, telegram_chat_id=1000)
        session.add_all([owner, other])
        await session.flush()
        session.add_all(
            [
                Item(
                    user_id=owner.id,
                    processing_status=ProcessingStatus.READY,
                    state=ItemState.ACTIVE,
                    source_type=SourceType.TEXT,
                    processing_stage="READY",
                    user_note="",
                    item_type=ItemType.LEARN,
                    title="My Android notes",
                    summary="Owner content.",
                    category="Android",
                    priority_score=50,
                ),
                Item(
                    user_id=other.id,
                    processing_status=ProcessingStatus.READY,
                    state=ItemState.ACTIVE,
                    source_type=SourceType.TEXT,
                    processing_stage="READY",
                    user_note="",
                    item_type=ItemType.LEARN,
                    title="Private other notes",
                    summary="Other content.",
                    category="Secrets",
                    priority_score=50,
                ),
            ]
        )
        await session.commit()

    sent = []
    edited = []
    callback_answers = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        callback_answers.append(text)
        return None

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    await on_navigation_callback(
        make_callback(42, "nav:categories"), settings, session_factory, FakeFSMContext()
    )
    category_markup = sent[0][1]["reply_markup"]
    category_button = next(
        button
        for row in category_markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("nav:category:")
    )
    assert category_button.callback_data == f"nav:category:{category_token('Android')}:page:0"
    assert "Secrets" not in sent[0][0]

    await on_navigation_callback(
        make_callback(42, category_button.callback_data),
        settings,
        session_factory,
        FakeFSMContext(),
    )
    assert "My Android notes" in edited[-1][0]
    assert "Private other notes" not in edited[-1][0]

    await on_navigation_callback(
        make_callback(42, "nav:category:stale-token"),
        settings,
        session_factory,
        FakeFSMContext(),
    )
    assert callback_answers[-1] == "Категория больше недоступна"


async def test_category_chooser_pages_past_twenty_categories(
    settings, session_factory, monkeypatch
):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        session.add_all(
            [
                Item(
                    user_id=user.id,
                    processing_status=ProcessingStatus.READY,
                    state=ItemState.ACTIVE,
                    source_type=SourceType.TEXT,
                    processing_stage="READY",
                    user_note="",
                    title=f"Item {index}",
                    category=f"Category {index:02}",
                )
                for index in range(25)
            ]
        )
        await session.commit()

    sent = []
    edited = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await on_category(make_message(42), settings, session_factory, "")
    assert (
        len(
            [
                button
                for row in sent[0][1]["reply_markup"].inline_keyboard
                for button in row
                if button.callback_data.startswith("nav:category:")
            ]
        )
        == 10
    )
    assert "первые 20" not in sent[0][0].lower()

    for page in (1, 2):
        await on_navigation_callback(
            make_callback(42, f"nav:categories:page:{page}"),
            settings,
            session_factory,
            FakeFSMContext(),
        )
    category_callbacks = []
    for _text, kwargs in [sent[0], *edited]:
        category_callbacks.extend(
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data.startswith("nav:category:")
        )
    assert len(category_callbacks) == len(set(category_callbacks)) == 25
    assert (
        len(
            [
                button
                for row in edited[-1][1]["reply_markup"].inline_keyboard
                for button in row
                if button.callback_data.startswith("nav:categories:page:")
            ]
        )
        == 1
    )


async def test_inbox_rows_page_through_all_twenty_seven_items(
    settings, session_factory, monkeypatch
):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        session.add_all(
            [
                Item(
                    user_id=user.id,
                    telegram_message_id=index,
                    source_index=0,
                    processing_status=ProcessingStatus.READY,
                    state=ItemState.ACTIVE,
                    source_type=SourceType.TEXT,
                    processing_stage="READY",
                    user_note="",
                    title=f"Item {index:02}",
                    created_at=datetime(2026, 9, 1) + timedelta(days=index),
                )
                for index in range(27)
            ]
        )
        await session.commit()
        expected_ids = list(
            (
                await session.scalars(
                    select(Item.id)
                    .where(Item.user_id == user.id)
                    .order_by(Item.created_at.desc(), Item.id.desc())
                )
            ).all()
        )

    sent = []
    edited = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await on_inbox(make_message(42), settings, session_factory)
    assert (
        len(
            [
                button
                for row in sent[0][1]["reply_markup"].inline_keyboard
                for button in row
                if button.callback_data.startswith("item:view:")
            ]
        )
        == 10
    )

    next_callback = next(
        button.callback_data
        for row in sent[0][1]["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data.startswith("nav:inbox:page:")
    )
    for _ in range(2):
        await on_navigation_callback(
            make_callback(42, next_callback), settings, session_factory, FakeFSMContext()
        )
        next_buttons = [
            button.callback_data
            for row in edited[-1][1]["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data.startswith("nav:inbox:page:")
        ]
        next_callback = next_buttons[-1] if next_buttons else ""

    seen_ids = []
    for _text, kwargs in [sent[0], *edited]:
        seen_ids.extend(
            int(button.callback_data.removeprefix("item:view:"))
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data.startswith("item:view:")
        )
    assert seen_ids == expected_ids
    assert len(set(seen_ids)) == 27


async def test_category_item_callbacks_page_through_all_twenty_seven_items(
    settings, session_factory, monkeypatch
):
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        session.add_all(
            [
                Item(
                    user_id=user.id,
                    telegram_message_id=index,
                    source_index=0,
                    processing_status=ProcessingStatus.READY,
                    state=ItemState.ACTIVE,
                    source_type=SourceType.TEXT,
                    processing_stage="READY",
                    user_note="",
                    title=f"Shared item {index:02}",
                    category="Shared",
                    priority_score=50,
                    created_at=datetime(2026, 9, 1) + timedelta(days=index),
                )
                for index in range(27)
            ]
        )
        await session.commit()

    sent = []
    edited = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await on_category(make_message(42), settings, session_factory, "Shared")
    visible_ids = []
    current_markup = sent[0][1]["reply_markup"]
    for page in range(3):
        visible_ids.extend(
            button.callback_data
            for row in current_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("item:view:")
        )
        next_callback = next(
            (
                button.callback_data
                for row in current_markup.inline_keyboard
                for button in row
                if button.text == "Ещё →"
            ),
            None,
        )
        if next_callback is None:
            break
        await on_navigation_callback(
            make_callback(42, next_callback), settings, session_factory, FakeFSMContext()
        )
        current_markup = edited[-1][1]["reply_markup"]

    assert len(visible_ids) == len(set(visible_ids)) == 27
    assert not any(
        button.text == "Ещё →" for row in current_markup.inline_keyboard for button in row
    )


async def test_malformed_page_callbacks_fail_safely_and_huge_pages_clamp(
    settings, session_factory, monkeypatch
):
    answers = []
    edits = []

    async def answer_callback(self, text=None, **kwargs):
        answers.append(text)

    async def edit_message(self, text, **kwargs):
        edits.append((text, kwargs))

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_text", edit_message)

    await on_navigation_callback(
        make_callback(42, "nav:inbox:page:not-a-page"),
        settings,
        session_factory,
        FakeFSMContext(),
    )
    assert answers[-1] == "Страница больше недоступна"

    await on_navigation_callback(
        make_callback(42, "nav:inbox:page:999999999999999999"),
        settings,
        session_factory,
        FakeFSMContext(),
    )
    assert edits[-1][0].startswith("📥 Inbox · 1\n\nНичего не найдено.")


async def test_menu_settings_exposes_attention_and_back_navigation(
    settings, session_factory, monkeypatch
):
    sent = []
    edited = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await on_navigation_callback(
        make_callback(42, "nav:settings"), settings, session_factory, FakeFSMContext()
    )
    settings_buttons = [
        button for row in sent[0][1]["reply_markup"].inline_keyboard for button in row
    ]
    assert any(button.callback_data == "settings:attention:open" for button in settings_buttons)
    assert {
        "settings:edit:timezone",
        "settings:edit:digest_time",
        "settings:edit:quiet_hours",
        "settings:digest",
    } <= {button.callback_data for button in settings_buttons}

    await on_attention_settings_callback(
        make_callback(42, "settings:attention:open"), settings, session_factory
    )
    assert "🧠 Attention" in edited[-1][0]
    attention_buttons = [
        button for row in edited[-1][1]["reply_markup"].inline_keyboard for button in row
    ]
    assert any(button.callback_data == "settings:open" for button in attention_buttons)
    assert any(button.callback_data == "settings:attention:status" for button in attention_buttons)

    await on_attention_settings_callback(
        make_callback(42, "settings:attention:status"), settings, session_factory
    )
    assert "📊 Attention сейчас" in edited[-1][0]
    status_buttons = [
        button for row in edited[-1][1]["reply_markup"].inline_keyboard for button in row
    ]
    assert any(
        button.text == "← Назад" and button.callback_data == "settings:attention:open"
        for button in status_buttons
    )
    assert any(
        button.text == "↻ Обновить" and button.callback_data == "settings:attention:status"
        for button in status_buttons
    )

    await on_settings_open_callback(make_callback(42, "settings:open"), settings, session_factory)
    assert "Ежедневная сводка" in edited[-1][0]


async def test_ask_prompt_cancel_clears_ephemeral_state_without_job(
    settings, session_factory, monkeypatch
):
    edited = []

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    state = FakeFSMContext()

    await on_navigation_callback(make_callback(42, "nav:ask"), settings, session_factory, state)
    assert state.value == GuidedInput.ask.state
    assert edited[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "nav:input:cancel"

    await on_navigation_callback(
        make_callback(42, "nav:input:cancel"), settings, session_factory, state
    )
    assert state.value is None
    assert edited[-1] == ("Отменено.", {"reply_markup": None})
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AskJob)) == 0


async def test_export_menu_and_no_argument_command_offer_both_modes(
    settings, session_factory, monkeypatch
):
    edited = []
    sent = []

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    await on_export(make_message(42, message_id=816, text="/export"), settings, session_factory)
    assert sent[-1][0] == "📦 Экспорт"
    assert {
        button.callback_data
        for row in sent[-1][1]["reply_markup"].inline_keyboard
        for button in row
    } == {"export:mode:COMPACT", "export:mode:FULL", "nav:menu"}

    await on_navigation_callback(
        make_callback(42, "nav:export", message_id=817),
        settings,
        session_factory,
        FakeFSMContext(),
    )
    assert edited[-1][0] == "📦 Экспорт"
    assert {
        button.callback_data
        for row in edited[-1][1]["reply_markup"].inline_keyboard
        for button in row
    } == {"export:mode:COMPACT", "export:mode:FULL", "nav:menu"}

    await on_export_mode_callback(
        make_callback(42, "export:mode:FULL", message_id=817), settings, session_factory
    )
    assert edited[-1][0] == "Готовлю полный экспорт…"
    assert edited[-1][1]["reply_markup"] is None
    await on_export(
        make_message(42, message_id=818, text="/export compact"), settings, session_factory
    )
    await on_export(
        make_message(42, message_id=819, text="/export full"), settings, session_factory
    )
    async with session_factory() as session:
        jobs = list((await session.scalars(select(ExportJob))).all())
        assert {(job.telegram_message_id, job.mode) for job in jobs} == {
            (817, "FULL"),
            (818, "COMPACT"),
            (819, "FULL"),
        }


async def test_attention_command_and_menu_share_chooser_and_count_projection(
    settings, session_factory, monkeypatch
):
    sent = []
    edited = []
    selected_limits = []

    async def answer_message(self, text, **kwargs):
        sent.append((text, kwargs))

    async def edit_message(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    async def capture_attention(*, arguments, **kwargs):
        selected_limits.append(arguments)

    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr("app.bot.handlers._send_attention_for_actor", capture_attention)

    await on_attention(make_message(42, text="/attention"), settings, session_factory)
    assert sent[-1][0] == "✨ Внимание\n\nСколько показать?"
    assert {
        button.callback_data
        for row in sent[-1][1]["reply_markup"].inline_keyboard
        for button in row
    } == {
        "nav:attention:show:1",
        "nav:attention:show:3",
        "nav:attention:show:5",
        "nav:attention:status",
        "settings:attention:open",
        "nav:menu",
    }
    await on_navigation_callback(
        make_callback(42, "nav:attention"), settings, session_factory, FakeFSMContext()
    )
    assert edited[-1][0] == sent[-1][0]
    assert len(edited[-1][1]["reply_markup"].inline_keyboard) == len(
        sent[-1][1]["reply_markup"].inline_keyboard
    )

    await on_attention(make_message(42, text="/attention 5"), settings, session_factory, "5")
    await on_navigation_callback(
        make_callback(42, "nav:attention:show:5"), settings, session_factory, FakeFSMContext()
    )
    assert selected_limits == ["5", "5"]


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
    from app.services.retrieval import list_category_items_page, list_inbox_page, search_items

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
            "📥 Inbox": (await list_inbox_page(session, user.id)).items,
            "Категория: Shared": (await list_category_items_page(session, user.id, "Shared")).items,
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
            line[2:].split(" — ", 1)[0] for line in text.splitlines() if line.startswith("• ")
        ] == [item.title for item in visible_items]
        buttons = [
            button
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("item:view:")
        ]
        assert [button.callback_data for button in buttons] == [
            f"item:view:{item.id}" for item in visible_items
        ]
        assert [button.text for button in buttons] == [item.title for item in visible_items]


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


async def test_settings_buttons_use_existing_validation_and_keep_invalid_input_state(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    edited = []

    async def edit_text(self, text, **kwargs):
        edited.append((text, kwargs))

    async def answer_callback(self, text=None, **kwargs):
        return None

    monkeypatch.setattr(Message, "edit_text", edit_text)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    await on_settings(make_message(42, text="/settings"), settings, session_factory)

    setting_buttons = {
        button.callback_data for row in settings_keyboard(True).inline_keyboard for button in row
    }
    assert {
        "settings:edit:timezone",
        "settings:edit:digest_time",
        "settings:edit:quiet_hours",
        "settings:digest",
        "settings:attention:open",
    } <= setting_buttons

    timezone_state = FakeFSMContext()
    await on_settings_edit_callback(
        make_callback(42, "settings:edit:timezone"), settings, session_factory, timezone_state
    )
    assert timezone_state.value == GuidedInput.settings_timezone.state
    assert "IANA timezone" in edited[-1][0]
    await on_guided_notification_setting_input(
        make_message(42, text="Mars/Phobos"),
        settings,
        session_factory,
        timezone_state,
        "timezone",
    )
    assert timezone_state.value == GuidedInput.settings_timezone.state
    current = await get_notification_settings(session_factory, 42)
    assert current[0].timezone == "UTC"
    assert "Неизвестный часовой пояс" in sent[-1]

    await on_guided_notification_setting_input(
        make_message(42, text="Europe/Moscow"),
        settings,
        session_factory,
        timezone_state,
        "timezone",
    )
    assert timezone_state.value is None
    assert "Europe/Moscow" in sent[-1]

    for callback_data, state_name, value, setting_name in (
        (
            "settings:edit:digest_time",
            GuidedInput.settings_digest_time,
            "08:45",
            "digest_time",
        ),
        (
            "settings:edit:quiet_hours",
            GuidedInput.settings_quiet_hours,
            "22:30-08:00",
            "quiet_hours",
        ),
    ):
        state = FakeFSMContext()
        await on_settings_edit_callback(
            make_callback(42, callback_data), settings, session_factory, state
        )
        assert state.value == state_name.state
        await on_guided_notification_setting_input(
            make_message(42, text=value), settings, session_factory, state, setting_name
        )
        assert state.value is None

    current = await get_notification_settings(session_factory, 42)
    assert current[1]["daily_digest_time"] == "08:45"
    assert current[1]["quiet_hours_start"] == "22:30"
    assert current[1]["quiet_hours_end"] == "08:00"


async def test_settings_attention_command_and_callbacks_preserve_current_choice(
    settings, session_factory, monkeypatch
):
    sent = capture_answers(monkeypatch)
    await on_settings(make_message(42), settings, session_factory, "attention")
    assert "🧠 Attention" in sent[0]
    assert "Normal" in sent[0]
    assert "Общие напоминания: включены" in sent[0]

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
    assert "Статус: выключен" in edited[-1][0]
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
    assert "Общие напоминания: выключены" in edited[-1][0]
    updated = await get_notification_settings(session_factory, 42)
    assert updated[1]["attention_enabled"] is False
    assert updated[1]["generic_motivation_enabled"] is False
    markup = edited[-1][1]["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(
        button.callback_data == "settings:attention:motivation"
        and button.text == "💬 Включить общие напоминания"
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
    assert (
        "profile" in names
        and "profile_update" in names
        and "settings_command" in names
        and "menu_command" in names
        and "guided_ask" in names
        and "guided_profile" in names
        and "guided_search" in names
    )
    assert {"help_command", "today", "attention", "inbox", "category", "search"} <= set(names)
    assert names.index("forwarded_text") < names.index("start")
    assert "item_action" in [h.callback.__name__ for h in router.callback_query.handlers]
    assert "settings_attention" in [h.callback.__name__ for h in router.callback_query.handlers]
    assert {
        "navigation",
        "export_mode",
        "settings_open",
    } <= {h.callback.__name__ for h in router.callback_query.handlers}


async def test_router_fsm_guidance_precedes_capture_and_commands_escape(
    settings, session_factory, monkeypatch
):
    bot = Bot("123456:TEST")
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(make_router(settings, session_factory))
    sent = []

    async def bot_call(self, method, request_timeout=None):
        api_method = method.__api_method__
        if api_method == "sendMessage":
            chat_id = method.chat_id
            text = method.text
            reply_markup = method.reply_markup
            message_id = 1000 + len(sent)
        elif api_method == "editMessageText":
            chat_id = method.chat_id
            text = method.text
            reply_markup = method.reply_markup
            message_id = method.message_id
        else:
            return True
        sent.append((chat_id, text))
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=TgUser(id=self.id, is_bot=True, first_name="AIInbox"),
            text=text,
            reply_markup=reply_markup,
        ).as_(self)

    async def edit_message(self, text, **kwargs):
        sent.append((self.chat.id, text))
        return self

    async def answer_callback(self, text=None, **kwargs):
        return True

    monkeypatch.setattr(Bot, "__call__", bot_call)
    monkeypatch.setattr(Message, "edit_text", edit_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    def bot_menu_message(message_id: int) -> Message:
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=42, type="private"),
            from_user=TgUser(id=bot.id, is_bot=True, first_name="AIInbox"),
            text="Главное меню:",
        ).as_(bot)

    async def tap(update_id: int, data: str) -> None:
        callback = CallbackQuery(
            id=f"callback-{update_id}",
            from_user=TgUser(id=42, is_bot=False, first_name="Test"),
            chat_instance="private",
            message=bot_menu_message(700),
            data=data,
        ).as_(bot)
        await dispatcher.feed_update(
            bot, Update(update_id=update_id, callback_query=callback).as_(bot)
        )

    async def message(update_id: int, message_id: int, text: str, *, origin=None) -> None:
        incoming = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=42, type="private"),
            from_user=TgUser(id=42, is_bot=False, first_name="Test"),
            text=text,
            forward_origin=origin,
        ).as_(bot)
        await dispatcher.feed_update(bot, Update(update_id=update_id, message=incoming).as_(bot))

    try:
        await tap(1, "nav:ask")
        await message(2, 801, "first guided question")
        async with session_factory() as session:
            jobs = list((await session.scalars(select(AskJob))).all())
            assert len(jobs) == 1
            assert jobs[0].telegram_message_id == 801
            assert await session.scalar(select(func.count()).select_from(Item)) == 0

        await tap(3, "nav:ask")
        await message(4, 802, "/today")
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(AskJob)) == 1
            assert await session.scalar(select(func.count()).select_from(Item)) == 0
        await message(5, 803, "ordinary capture after command")

        await tap(6, "nav:ask")
        origin = MessageOriginChannel(
            type="channel",
            date=datetime.now(UTC),
            chat=Chat(id=-1001, type="channel", title="Source"),
            message_id=804,
        )
        await message(7, 805, "/today forwarded text", origin=origin)
        async with session_factory() as session:
            forwarded_item = await session.scalar(
                select(Item).where(Item.telegram_message_id == 805)
            )
            assert forwarded_item is not None
            forwarded_content = await session.scalar(
                select(Content).where(Content.item_id == forwarded_item.id)
            )
            assert forwarded_content.text == "/today forwarded text"
            assert await session.scalar(select(func.count()).select_from(AskJob)) == 1

        await message(8, 806, "second guided question")
        async with session_factory() as session:
            jobs = list((await session.scalars(select(AskJob).order_by(AskJob.id))).all())
            assert len(jobs) == 2
            assert jobs[-1].telegram_message_id == 806
            assert await session.scalar(select(func.count()).select_from(Item)) == 2

        await tap(9, "nav:profile:edit")
        await message(10, 807, "сместить фокус профиля на локальные модели")
        async with session_factory() as session:
            profile_job = await session.scalar(select(ProfileUpdateJob))
            assert profile_job is not None
            assert profile_job.instruction == "сместить фокус профиля на локальные модели"
            assert profile_job.status == "PENDING"
            assert await session.scalar(select(func.count()).select_from(Item)) == 2

        await tap(11, "nav:profile:edit")
        await message(12, 808, "/today")
        async with session_factory() as session:
            jobs = list((await session.scalars(select(ProfileUpdateJob))).all())
            assert len(jobs) == 1
            assert jobs[0].instruction != "/today"
            assert await session.scalar(select(func.count()).select_from(Item)) == 2

        await message(13, 809, "ordinary capture after profile command escape")
        await tap(14, "nav:profile:edit")
        await tap(15, "nav:input:cancel")
        await message(16, 810, "ordinary capture after profile cancel")
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(ProfileUpdateJob)) == 1
            assert await session.scalar(select(func.count()).select_from(Item)) == 4
    finally:
        await bot.session.close()


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
