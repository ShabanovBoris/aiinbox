import json
from functools import partial

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import SimpleEventIsolation
from sqlalchemy import func, select

from app.bot.handlers import GuidedInput, make_router, on_item_widget_callback
from app.bot.item_widgets import (
    INBOX_ARCHIVED_NOTICE,
    inbox_widget_screen,
    widget_update_middleware,
)
from app.bot.notify import send_item_failure, send_item_result
from app.bot.presentation import ItemNavigationEntry
from app.domain.enums import ContentKind, ItemState, ProcessingStatus, SourceType
from app.main import _start_polling
from app.services.retrieval import ItemPage
from app.storage.models import Content, Event, Item, ItemSource, User


async def _seed(session_factory, *, actor=42, summary="Короткая сводка"):
    async with session_factory() as session:
        user = User(telegram_user_id=actor, telegram_chat_id=actor)
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            telegram_message_id=17,
            state=ItemState.ACTIVE,
            processing_status=ProcessingStatus.READY,
            source_type=SourceType.WEB,
            source_url="https://example.org/article",
            title="Материал",
            summary=summary,
            user_note="",
        )
        session.add(item)
        await session.commit()
        return item


def _widget_update(query_id, data, *, actor=42, chat_id=42):
    return {
        "update_id": 17,
        "widget_query": {
            "id": query_id,
            "from": {"id": actor, "is_bot": False, "first_name": "Tester"},
            "chat": {"id": chat_id, "type": "private"},
            "message_id": 701,
            "chat_instance": "test-chat",
            "type": {"type": "data"},
            "data": data,
        },
    }


def _inbox_command(text="/inbox"):
    return {
        "update_id": 18,
        "message": {
            "message_id": 700,
            "date": 1234,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }


@pytest.fixture
async def widget_bot(settings, session_factory, monkeypatch):
    settings = settings.model_copy(update={"telegram_item_widgets_enabled": True})
    bot = Bot("123456:TEST")
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(make_router(settings, session_factory))
    dispatcher.update.outer_middleware(
        widget_update_middleware(
            partial(on_item_widget_callback, settings=settings, session_factory=session_factory)
        )
    )
    calls = []

    async def bot_call(self, method, request_timeout=None):
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", bot_call)
    try:
        yield bot, dispatcher, calls
    finally:
        await bot.session.close()


async def test_text_card_opens_and_legacy_widget_snooze_replay_keeps_one_deadline(
    widget_bot, session_factory
):
    bot, dispatcher, calls = widget_bot
    summary = "Короткая сводка </script> 💡"
    item = await _seed(session_factory, summary=summary)
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": 1,
            "callback_query": {
                "id": "open-card",
                "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
                "chat_instance": "test-chat",
                "message": {
                    "message_id": 700,
                    "date": 1234,
                    "chat": {"id": 42, "type": "private"},
                },
                "data": f"item:view:{item.id}",
            },
        },
    )
    card = calls[0]
    assert card.__api_method__ == "sendMessage"
    assert card.text == f"Материал\n\nСводка\n{summary}"
    assert [entity.type for entity in card.entities] == ["bold", "bold"]
    assert card.link_preview_options.is_disabled
    buttons = {
        button.callback_data or button.url: button
        for row in card.reply_markup.inline_keyboard
        for button in row
    }
    assert {
        f"item:{action}:{item.id}" for action in ("archive", "done", "later", "original")
    } <= buttons.keys()
    assert buttons[f"item:archive:{item.id}"].text == "В архив"
    assert buttons[f"item:archive:{item.id}"].style == "danger"
    assert buttons[f"item:done:{item.id}"].text == "Сделано"
    assert buttons["https://example.org/article"].text == "Статья — example.org"
    assert f"item:more:{item.id}" not in buttons

    state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    await state.set_state(GuidedInput.ask)
    await dispatcher.feed_raw_update(bot, _widget_update("choose-time", f"item:later:{item.id}"))
    assert await state.get_state() is None
    menu = json.loads(calls[-1].data)["screen"]
    assert menu["text"] == "Когда напомнить?"
    assert ["Завтра", f"item:snooze:{item.id}:tomorrow"] in menu["rows"][0]

    query = _widget_update("snooze", f"item:snooze:{item.id}:tomorrow")
    await dispatcher.feed_raw_update(bot, query)
    answer, edit = calls[-2:]
    assert answer.__api_method__ == "answerWidgetQuery"
    assert edit.__api_method__ == "editMessageWidgetData"
    assert edit.__returning__ is bool
    assert edit.chat_id == 42 and edit.message_id == 701
    assert json.loads(edit.chat_data) == json.loads(answer.data)["screen"]

    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.state is ItemState.SNOOZED
        first_deadline = stored.snoozed_until

    await dispatcher.feed_raw_update(bot, query)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.snoozed_until == first_deadline
        assert (
            await session.scalar(
                select(func.count()).where(Event.item_id == item.id, Event.event_type == "SNOOZED")
            )
            == 1
        )


async def test_widget_archive_and_feedback_use_owner_scope(widget_bot, session_factory):
    bot, dispatcher, calls = widget_bot
    item = await _seed(session_factory)
    await _seed(session_factory, actor=1000)
    action = f"item:archive:{item.id}"
    for actor, chat_id in ((1000, 1000), (1000, 42)):
        await dispatcher.feed_raw_update(
            bot, _widget_update(f"denied-{chat_id}", action, actor=actor, chat_id=chat_id)
        )
        assert calls[-1].__api_method__ == "answerWidgetQuery"
        assert json.loads(calls[-1].data)["notice"]

    feedback = _widget_update("useful", f"feedback:useful:{item.id}")
    await dispatcher.feed_raw_update(bot, feedback)
    await dispatcher.feed_raw_update(bot, feedback)
    await dispatcher.feed_raw_update(bot, _widget_update("archive", action))
    screen = json.loads(calls[-1].chat_data)
    assert screen == {"text": "В архиве", "rows": [[["Сохранённое", "nav:inbox"]]]}
    async with session_factory() as session:
        assert (await session.get(Item, item.id)).state is ItemState.ARCHIVED
        events = await session.scalars(select(Event.event_type).where(Event.item_id == item.id))
        assert list(events).count("USEFUL") == 1

    await dispatcher.feed_raw_update(bot, _widget_update("return", "nav:inbox"))
    assert calls[-2].__api_method__ == "sendWidget"
    page = json.loads(calls[-2].html.split("let page = ", 1)[1].split(";\n", 1)[0])
    assert page["rows"] == []


async def test_inbox_widget_opens_cards_and_archives_in_place(widget_bot, session_factory):
    bot, dispatcher, calls = widget_bot
    target = await _seed(session_factory)
    foreign = await _seed(session_factory, actor=1000)
    async with session_factory() as session:
        other_items = [
            Item(
                user_id=target.user_id,
                source_type=SourceType.TEXT,
                state=ItemState.ACTIVE,
                title=f"Материал {index}",
                user_note="",
            )
            for index in range(9)
        ]
        done = Item(
            user_id=target.user_id,
            source_type=SourceType.TEXT,
            state=ItemState.DONE,
            title="Выполненная запись",
            user_note="",
        )
        session.add_all([*other_items, done])
        await session.commit()

    await dispatcher.feed_raw_update(bot, _inbox_command())
    inbox = calls[-1]
    assert inbox.__api_method__ == "sendWidget"
    page = json.loads(inbox.html.split("let page = ", 1)[1].split(";\n", 1)[0])
    assert page["number"] == 0 and page["next"]
    assert len(page["rows"]) == 10
    assert [str(done.id), done.title, False, False] in page["rows"]

    await dispatcher.feed_raw_update(bot, _widget_update("next", "inbox:page:saved:1"))
    edit, answer = calls[-2:]
    assert edit.__api_method__ == "editMessageWidgetData" and edit.message_id == 701
    page = json.loads(edit.chat_data)["inbox"]
    assert page["rows"] == [[str(target.id), target.title, True, True]]
    assert json.loads(answer.data)["screen"]["inbox"] == page

    await dispatcher.feed_raw_update(bot, _widget_update("open", f"item:view:{target.id}"))
    card, answer = calls[-2:]
    assert card.__api_method__ == "sendMessage" and target.title in card.text
    assert json.loads(answer.data)["screen"] is None

    await dispatcher.feed_raw_update(bot, _widget_update("original", f"item:original:{target.id}"))
    copy, answer = calls[-2:]
    assert copy.__api_method__ == "copyMessage"
    assert (copy.chat_id, copy.from_chat_id, copy.message_id) == (
        42,
        42,
        target.telegram_message_id,
    )
    assert json.loads(answer.data) == {"screen": None, "notice": "Оригинал отправлен"}

    await dispatcher.feed_raw_update(
        bot, _widget_update("foreign", f"inbox:archive:{foreign.id}:1")
    )
    assert json.loads(calls[-1].data)["screen"] is None

    archive = _widget_update("archive-last", f"inbox:archive:{target.id}:1")
    for _ in range(2):
        await dispatcher.feed_raw_update(bot, archive)
        edit, answer = calls[-2:]
        assert edit.__api_method__ == "editMessageWidgetData" and edit.message_id == 701
        page = json.loads(edit.chat_data)["inbox"]
        assert page["number"] == 0 and not page["next"]
        assert str(target.id) not in {row[0] for row in page["rows"]}
        assert json.loads(answer.data)["notice"] == INBOX_ARCHIVED_NOTICE

    await dispatcher.feed_raw_update(bot, _widget_update("show-archive", "inbox:page:archive:0"))
    page = json.loads(calls[-2].chat_data)["inbox"]
    assert page["archived"] and page["rows"] == [[str(target.id), target.title, False, True]]
    async with session_factory() as session:
        assert (await session.get(Item, target.id)).state is ItemState.ARCHIVED
        assert (await session.get(Item, foreign.id)).state is ItemState.ACTIVE
        assert (
            await session.scalar(
                select(func.count()).where(
                    Event.item_id == target.id, Event.event_type == "ARCHIVED"
                )
            )
            == 1
        )

    await dispatcher.feed_raw_update(bot, _inbox_command("/inbox archive"))
    assert calls[-1].__api_method__ == "sendWidget"
    opened_archive = json.loads(calls[-1].html.split("let page = ", 1)[1].split(";\n", 1)[0])
    assert opened_archive == page

    await dispatcher.feed_raw_update(bot, _inbox_command("/inbox plain"))
    assert calls[-1].__api_method__ == "sendMessage"


def test_inbox_widget_preserves_every_id_with_long_unicode_titles():
    ids = [2**63 - 1 - index for index in range(10)]
    page = ItemPage(
        items=tuple(Item(id=item_id, state=ItemState.ACTIVE) for item_id in ids),
        page=1,
        has_previous=True,
        has_next=True,
    )
    entries = [ItemNavigationEntry(item_id, 'заголовок 🧭 "' * 5) for item_id in ids]
    screen = inbox_widget_screen(page, entries, archived=False)
    result = {"screen": screen, "notice": INBOX_ARCHIVED_NOTICE}
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert [row[0] for row in screen["inbox"]["rows"]] == [str(item_id) for item_id in ids]
    assert all(row[1].endswith("…") for row in screen["inbox"]["rows"])


async def test_widget_category_pages_fit_and_keep_the_original_category(
    widget_bot, session_factory
):
    bot, dispatcher, calls = widget_bot
    item = await _seed(session_factory)
    async with session_factory() as session:
        for index in range(6):
            session.add(
                Item(
                    user_id=item.user_id,
                    source_type=SourceType.TEXT,
                    category=f"👍 {'🧭' * 55} {index:02}",
                    user_note="",
                )
            )
        await session.commit()

    await dispatcher.feed_raw_update(
        bot, _widget_update("category-menu", f"feedback:category_menu:{item.id}")
    )
    assert len(calls[-1].data.encode()) <= 1024
    rows = json.loads(calls[-1].data)["screen"]["rows"]
    next_page = next(value for row in rows for label, value in row if label == "Ещё")
    await dispatcher.feed_raw_update(bot, _widget_update("category-page-two", next_page))
    rows = json.loads(calls[-1].data)["screen"]["rows"]
    assert len(calls[-1].data.encode()) <= 1024
    assert rows[0][0][0].startswith("👍 ")
    category = next(
        value for row in rows for _, value in row if value.startswith("feedback:category:")
    )
    await dispatcher.feed_raw_update(bot, _widget_update("change-category", category))
    assert json.loads(calls[-1].data)["notice"] == "Категория изменена"
    async with session_factory() as session:
        assert (await session.get(Item, item.id)).category == f"👍 {'🧭' * 55} 04"


async def test_text_card_retries_deleted_reply_without_changing_content(
    widget_bot, session_factory, monkeypatch
):
    bot, _, calls = widget_bot
    item = await _seed(session_factory)

    async def bot_call(self, method, request_timeout=None):
        calls.append(method)
        if method.reply_parameters:
            raise TelegramBadRequest(method=method, message="reply message not found")
        return True

    monkeypatch.setattr(Bot, "__call__", bot_call)
    await send_item_result(bot, session_factory, item)
    assert [method.__api_method__ for method in calls] == ["sendMessage", "sendMessage"]
    assert calls[0].reply_parameters.message_id == 17 and calls[1].reply_parameters is None
    assert calls[0].text == calls[1].text
    assert calls[0].entities == calls[1].entities


async def test_text_card_shows_only_summary_when_source_content_exists(widget_bot, session_factory):
    bot, dispatcher, calls = widget_bot
    summary = "\n\n".join(f"Абзац {i}. " + "Длинная сводка " * 15 for i in range(4)).strip()
    item = await _seed(session_factory, summary=summary)
    async with session_factory() as session:
        session.add_all(
            [
                Content(item_id=item.id, kind=kind, text=f"Исходный материал {kind.value} " * 200)
                for kind in ContentKind
            ]
        )
        await session.commit()

    await send_item_result(bot, session_factory, item)
    await dispatcher.feed_raw_update(bot, _widget_update("open", f"item:view:{item.id}"))
    notification, opened = [method for method in calls if method.__api_method__ == "sendMessage"]
    for card in (notification, opened):
        assert card.text == f"Материал\n\nСводка\n{summary}"
        assert [entity.type for entity in card.entities] == ["bold", "bold"]
        assert card.link_preview_options.is_disabled
        assert f"item:full_text:{item.id}" not in [
            button.callback_data for row in card.reply_markup.inline_keyboard for button in row
        ]


async def test_old_full_text_button_refreshes_only_the_owners_card(widget_bot, session_factory):
    bot, dispatcher, calls = widget_bot
    item = await _seed(session_factory)
    saved_text = "🙂" * 2050 + "конец страницы"
    async with session_factory() as session:
        session.add(Content(item_id=item.id, kind=ContentKind.WEB_TEXT, text=saved_text))
        await session.commit()

    action = f"item:full_text:{item.id}"
    for actor, chat in [(42, 1000), (1000, 1000), (42, 42)]:
        update = _inbox_command()
        message = update.pop("message")
        message["chat"]["id"] = chat
        update["callback_query"] = {
            "id": f"full-{actor}-{chat}",
            "from": {"id": actor, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "test-chat",
            "message": message,
            "data": action,
        }
        await dispatcher.feed_raw_update(bot, update)
    (card,) = [method for method in calls if method.__api_method__ == "editMessageText"]
    assert card.chat_id == 42
    assert card.text == f"Материал\n\nСводка\n{item.summary}"
    assert action not in [
        button.callback_data for row in card.reply_markup.inline_keyboard for button in row
    ]
    assert all(method.__api_method__ != "sendDocument" for method in calls)


async def test_oversized_legacy_summary_is_truncated_without_splitting_emoji(
    widget_bot, session_factory
):
    bot, _, calls = widget_bot
    item = await _seed(session_factory, summary="🙂" * 4096)

    await send_item_result(bot, session_factory, item)

    (card,) = calls
    assert card.text.startswith("Материал\n\nСводка\n🙂")
    assert card.text.endswith("🙂…")
    assert len(card.text.encode("utf-16-le")) // 2 <= 4096
    assert [entity.type for entity in card.entities] == ["bold", "bold"]


async def test_text_card_marks_failure_and_styles_recovery(widget_bot, session_factory):
    bot, _, calls = widget_bot
    item = await _seed(session_factory)
    item.processing_status = ProcessingStatus.FAILED
    item.error_code = "DOWNLOAD_FAILED"

    await send_item_failure(bot, session_factory, item)
    card = calls[-1]
    assert card.__api_method__ == "sendMessage"
    assert card.text.startswith("Материал\nОшибка обработки")
    assert "Не удалось загрузить содержимое ссылки." in card.text
    assert [entity.type for entity in card.entities] == ["bold", "code"]
    buttons = {b.callback_data: b for row in card.reply_markup.inline_keyboard for b in row}
    assert buttons[f"item:retry:{item.id}"].text == "Повторить"
    assert buttons[f"item:retry:{item.id}"].style == "primary"
    assert buttons[f"item:archive:{item.id}"].style == "danger"


async def test_text_card_keeps_partial_retry_eligibility(widget_bot, session_factory):
    bot, _, calls = widget_bot
    item = await _seed(session_factory)
    item.analysis_completeness = "PARTIAL"
    async with session_factory() as session:
        source = ItemSource(
            item_id=item.id, source_index=0, source_type=SourceType.WEB, extraction_status="FAILED"
        )
        session.add(source)
        await session.commit()

    await send_item_result(bot, session_factory, item)
    card = calls[-1]
    assert "Частично обработано" in card.text and item.summary in card.text
    assert f"item:retry:{item.id}" in [
        b.callback_data for row in card.reply_markup.inline_keyboard for b in row
    ]

    async with session_factory() as session:
        current = await session.get(ItemSource, source.id)
        current.metadata_json = {"failure_permanent": True}
        await session.commit()
    await send_item_result(bot, session_factory, item)
    card = calls[-1]
    assert "Частично обработано" in card.text
    assert all(
        b.callback_data != f"item:retry:{item.id}"
        for row in card.reply_markup.inline_keyboard
        for b in row
    )


async def test_widget_polling_keeps_normal_update_types(widget_bot, monkeypatch):
    bot, dispatcher, _ = widget_bot
    observed = {}

    async def start_polling(self, bot, **kwargs):
        observed.update(kwargs)

    monkeypatch.setattr(Dispatcher, "start_polling", start_polling)
    await _start_polling(dispatcher, bot, widget_queries=True)
    assert {"message", "callback_query", "widget_query"} <= set(observed["allowed_updates"])
