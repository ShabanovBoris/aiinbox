from types import SimpleNamespace

from sqlalchemy import func, select

from app.bot.handlers import on_item_callback
from app.bot.keyboards import (
    item_details_keyboard,
    item_interest_keyboard,
    item_keyboard,
    item_more_keyboard,
    item_sources_keyboard,
)
from app.bot.presentation import source_url_label
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.delivery import ITEM_VIDEO_PREFIX
from app.storage.models import Delivery, Event, Item, ItemSource, Reminder, User


class FakeCallbackMessage:
    """Capture Telegram edits so callback behavior can be verified offline."""

    def __init__(self):
        self.text = None
        self.reply_markup = None

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class FakeCallback:
    """Minimal CallbackQuery boundary used by the handler-level PM-01 tests."""

    def __init__(self, user_id: int, data: str):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = FakeCallbackMessage()
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


def _ready_item(**overrides) -> Item:
    """Create the smallest READY projection required by result formatting."""
    values = {
        "id": 7,
        "user_id": 1,
        "processing_status": ProcessingStatus.READY,
        "state": ItemState.ACTIVE,
        "source_type": SourceType.TEXT,
        "processing_stage": "READY",
        "user_note": "x",
        "title": "PM-01",
        "category": "Test",
        "item_type": ItemType.LEARN,
        "priority_score": 55,
        "interest_level": 2,
    }
    values.update(overrides)
    return Item(**values)


async def _persist_ready_item(session_factory) -> int:
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        item = _ready_item(id=None, user_id=user.id)
        session.add(item)
        await session.commit()
        return item.id


def test_ready_primary_keyboard_only_exposes_more_without_admin_rows():
    markup = item_keyboard(_ready_item())
    assert [[button.text for button in row] for row in markup.inline_keyboard] == [["••• Ещё"]]
    assert _callback_data(markup) == ["item:more:7"]


def _callback_data(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def test_interest_submenu_projects_current_level_and_details_are_read_only():
    item = _ready_item(interest_level=2)
    markup = item_interest_keyboard(item)
    assert [row[0].text for row in markup.inline_keyboard] == [
        "1 — Низкий",
        "2 — Обычный ✓",
        "3 — Высокий",
        "← Назад",
    ]
    assert _callback_data(markup) == [
        "item:interest:7:1",
        "item:interest:7:2",
        "item:interest:7:3",
        "item:more:7",
    ]
    assert _callback_data(item_details_keyboard(7)) == ["item:back:7"]


def test_more_menu_contains_secondary_controls_and_returns_to_primary():
    item = _ready_item()
    markup = item_more_keyboard(item)
    callbacks = _callback_data(markup)
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert {"✅ Готово", "⏰ Позже", "🗄 Архив", "⭐ Интерес"} <= set(labels)
    assert {"🛠 Обратная связь", "ℹ️ Детали", "← Назад"} <= set(labels)
    assert {
        "item:done:7",
        "item:later:7",
        "item:archive:7",
        "item:interest_menu:7",
        "feedback:menu:7",
        "item:details:7",
        "item:back:7",
    } <= set(callbacks)


def test_source_url_labels_name_destination_and_reject_unsafe_or_invalid_urls():
    cases = [
        (SourceType.YOUTUBE, "https://www.youtube.com/watch?v=x", "↗ YouTube"),
        (SourceType.INSTAGRAM, "https://www.instagram.com/reel/x", "↗ Instagram Reel"),
        (SourceType.WEB, "https://www.github.com/org/repo?q=secret", "↗ GitHub"),
        (SourceType.WEB, "https://www.Example.com/a?tracking=1", "↗ Статья — example.com"),
        (SourceType.DOCUMENT, "https://docs.example.com/file.pdf", "↗ Документ — docs.example.com"),
        (SourceType.TEXT, "https://www.example.com/path", "↗ example.com"),
    ]
    for source_type, url, expected in cases:
        assert source_url_label(source_type, url) == expected
    for url in (
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https:///no-host",
        "https://[broken",
        "https://user:secret@example.com/path",
        "http://localhost/page",
        "https://api.localhost/",
        "http://router.local/",
        "http://intranet/",
        "http://127.0.0.1/page",
        "https://192.168.1.12/private",
        "http://169.254.1.1/metadata",
        "http://[::1]/page",
        "http://[fc00::1]/page",
        "http://100.64.0.2/page",
        "http://192.0.0.9/page",
    ):
        assert source_url_label(SourceType.WEB, url) is None
    assert source_url_label(SourceType.WEB, "https://8.8.8.8/path") == "↗ Статья — 8.8.8.8"


def test_primary_sources_are_bounded_and_full_actions_stay_in_sources_menu():
    item = _ready_item()
    sources = [
        ItemSource(
            id=30 + index,
            item_id=item.id,
            source_index=index,
            source_type=SourceType.YOUTUBE if index < 3 else SourceType.WEB,
            source_url=(
                f"https://youtube.com/watch?v={index}"
                if index < 3
                else f"https://example.com/article/{index}"
            ),
            extraction_status="READY",
        )
        for index in range(6)
    ]

    primary = item_keyboard(item, sources)
    primary_callbacks = _callback_data(primary)
    source_actions = [
        button
        for row in primary.inline_keyboard
        for button in row
        if button.url
        or (button.callback_data or "").startswith("item:video:")
        or button.callback_data == f"item:sources:{item.id}"
    ]
    assert len(source_actions) <= 2
    assert len([value for value in primary_callbacks if value.startswith("item:video:")]) == 1
    assert f"item:sources:{item.id}" in primary_callbacks

    source_menu = item_sources_keyboard(item, sources)
    menu_buttons = [button for row in source_menu.inline_keyboard for button in row]
    assert len(menu_buttons) - 1 <= 12
    assert [
        button.callback_data
        for button in menu_buttons
        if button.callback_data and button.callback_data.startswith("item:video:")
    ] == [
        "item:video:7:30",
        "item:video:7:31",
        "item:video:7:32",
    ]
    assert [button.text for button in menu_buttons if button.url] == [
        "↗ YouTube 1",
        "↗ YouTube 2",
        "↗ YouTube 3",
        "↗ Статья — example.com 1",
        "↗ Статья — example.com 2",
        "↗ Статья — example.com 3",
    ]


def test_item_menu_callback_payloads_fit_telegram_limit():
    large_id = 9_223_372_036_854_775_807
    item = _ready_item(id=large_id)
    markups = [
        item_keyboard(item),
        item_more_keyboard(item),
        item_interest_keyboard(item),
        item_details_keyboard(large_id),
        item_sources_keyboard(item),
    ]
    for markup in markups:
        assert all(len(value.encode("utf-8")) <= 64 for value in _callback_data(markup))


def test_partial_ready_keyboard_exposes_retry():
    source = ItemSource(
        id=11,
        item_id=7,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://example.com/retry",
        extraction_status="FAILED",
        metadata_json={"failure_permanent": False},
    )
    markup = item_keyboard(_ready_item(analysis_completeness="PARTIAL"), [source])
    callbacks = _callback_data(markup)

    assert "item:retry:7" in callbacks


def test_partial_ready_keyboard_hides_retry_for_permanent_source_failure():
    source = ItemSource(
        id=12,
        item_id=7,
        source_index=0,
        source_type=SourceType.VIDEO,
        extraction_status="FAILED",
        error_code="TOO_LARGE",
        metadata_json={"failure_permanent": True},
    )
    markup = item_keyboard(_ready_item(analysis_completeness="PARTIAL"), [source])
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert "item:retry:7" not in callbacks


def test_security_rejected_source_url_stays_hidden_when_text_makes_item_ready():
    url = "https://example.com/private-network"
    source = ItemSource(
        id=13,
        item_id=7,
        source_index=0,
        source_type=SourceType.WEB,
        source_url=url,
        extraction_status="FAILED",
        error_code="SECURITY_REJECTED",
        metadata_json={"failure_permanent": True},
    )
    item = _ready_item(
        source_type=SourceType.WEB,
        source_url=url,
        analysis_completeness="PARTIAL",
    )

    for markup in (item_keyboard(item, [source]), item_sources_keyboard(item, [source])):
        assert all(button.url != url for row in markup.inline_keyboard for button in row)


def test_multi_url_keyboard_exposes_each_child_source():
    sources = [
        ItemSource(
            id=21,
            item_id=7,
            source_index=0,
            source_type=SourceType.WEB,
            source_url="https://example.com/first",
            extraction_status="READY",
        ),
        ItemSource(
            id=22,
            item_id=7,
            source_index=1,
            source_type=SourceType.WEB,
            source_url="https://example.com/second",
            extraction_status="READY",
        ),
    ]
    markup = item_keyboard(_ready_item(source_url=None), sources)
    links = [
        (button.text, button.url) for row in markup.inline_keyboard for button in row if button.url
    ]

    assert ("↗ Статья — example.com 1", "https://example.com/first") in links
    assert ("↗ Статья — example.com 2", "https://example.com/second") in links


def test_ready_keyboard_exposes_each_ready_youtube_and_instagram_source():
    """Project one explicit send action per ready child media source."""
    sources = [
        ItemSource(
            id=31,
            item_id=7,
            source_index=0,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=first",
            extraction_status="READY",
        ),
        ItemSource(
            id=32,
            item_id=7,
            source_index=1,
            source_type=SourceType.INSTAGRAM,
            source_url="https://www.instagram.com/reel/second/",
            extraction_status="READY",
        ),
        ItemSource(
            id=33,
            item_id=7,
            source_index=2,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=failed",
            extraction_status="FAILED",
        ),
    ]

    buttons = [
        button
        for row in item_sources_keyboard(_ready_item(), sources).inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("item:video:")
    ]

    assert [(button.text, button.callback_data) for button in buttons] == [
        ("📩 Прислать YouTube", "item:video:7:31"),
        ("📩 Прислать Reel", "item:video:7:32"),
    ]


async def test_video_callback_enqueues_one_source_scoped_delivery(settings, session_factory):
    """The callback persists a user-scoped outbox intent instead of downloading inline."""
    item_id = await _persist_ready_item(session_factory)
    async with session_factory() as session:
        source = ItemSource(
            item_id=item_id,
            source_index=0,
            source_type=SourceType.YOUTUBE,
            source_url="https://www.youtube.com/watch?v=ready",
            extraction_status="READY",
        )
        session.add(source)
        await session.commit()
        source_id = source.id
        sibling = ItemSource(
            item_id=item_id,
            source_index=1,
            source_type=SourceType.INSTAGRAM,
            source_url="https://www.instagram.com/reel/sibling/",
            extraction_status="READY",
        )
        session.add(sibling)
        await session.commit()
        sibling_id = sibling.id

    callback = FakeCallback(42, f"item:video:{item_id}:{source_id}")
    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"{ITEM_VIDEO_PREFIX}{source_id}",
            )
        )
        assert delivery is not None
        assert delivery.status == "PENDING"
        assert delivery.payload_json == {"source_id": source_id}
    assert callback.answers == ["Поставил видео в очередь"]

    duplicate = FakeCallback(42, f"item:video:{item_id}:{source_id}")
    await on_item_callback(duplicate, settings, session_factory)
    assert duplicate.answers == ["Видео уже готовится"]

    sibling_callback = FakeCallback(42, f"item:video:{item_id}:{sibling_id}")
    await on_item_callback(sibling_callback, settings, session_factory)
    assert sibling_callback.answers == ["Поставил видео в очередь"]
    async with session_factory() as session:
        deliveries = list(
            (
                await session.scalars(
                    select(Delivery).where(Delivery.item_id == item_id).order_by(Delivery.type)
                )
            ).all()
        )
    assert {delivery.type for delivery in deliveries} == {
        f"{ITEM_VIDEO_PREFIX}{source_id}",
        f"{ITEM_VIDEO_PREFIX}{sibling_id}",
    }


async def test_video_callback_cannot_enqueue_another_users_source(settings, session_factory):
    """Source ids in callback data never bypass the Item ownership boundary."""
    item_id = await _persist_ready_item(session_factory)
    async with session_factory() as session:
        source = ItemSource(
            item_id=item_id,
            source_index=0,
            source_type=SourceType.INSTAGRAM,
            source_url="https://www.instagram.com/reel/private/",
            extraction_status="READY",
        )
        session.add(source)
        await session.commit()
        source_id = source.id

    callback = FakeCallback(1000, f"item:video:{item_id}:{source_id}")
    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        count = await session.scalar(select(func.count(Delivery.id)))
    assert count == 0
    assert callback.answers == ["Это видео сейчас недоступно"]


async def test_interest_callback_updates_persisted_state_and_existing_message(
    settings, session_factory
):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:interest:{item_id}:3")
    callback.message.text = "✓ Сохранено\n\n🎯 PM-01"
    callback.message.reply_markup = item_interest_keyboard(_ready_item())
    original_text = callback.message.text

    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.interest_level == 3
    assert callback.message.text == original_text
    assert [row[0].text for row in callback.message.reply_markup.inline_keyboard[:3]] == [
        "1 — Низкий",
        "2 — Обычный",
        "3 — Высокий ✓",
    ]
    assert callback.answers == ["Интерес обновлён"]


async def test_more_navigation_is_owner_scoped_and_presentation_only(settings, session_factory):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:more:{item_id}")
    callback.message.text = "✓ Сохранено\n\n🎯 PM-01"
    callback.message.reply_markup = item_keyboard(_ready_item())

    await on_item_callback(callback, settings, session_factory)

    labels = {
        button.text for row in callback.message.reply_markup.inline_keyboard for button in row
    }
    assert {"✅ Готово", "⏰ Позже", "🗄 Архив", "⭐ Интерес"} <= labels
    assert {"🛠 Обратная связь", "ℹ️ Детали", "← Назад"} <= labels
    assert callback.message.text == "✓ Сохранено\n\n🎯 PM-01"
    assert callback.answers == [None]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 0
        assert await session.scalar(select(func.count(Reminder.id))) == 0


async def test_details_reload_is_read_only_and_item_owner_scoped(settings, session_factory):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:details:{item_id}")

    await on_item_callback(callback, settings, session_factory)

    assert callback.message.text.startswith("ℹ️ Детали")
    assert "Категория: Test" in callback.message.text
    assert "Тип: Изучить" in callback.message.text
    assert [
        button.text for row in callback.message.reply_markup.inline_keyboard for button in row
    ] == ["← Назад"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 0

    for action in ("more", "details", "sources", "interest_menu", "back"):
        other_user = FakeCallback(1000, f"item:{action}:{item_id}")
        await on_item_callback(other_user, settings, session_factory)
        assert other_user.answers == ["Item недоступен"]
        assert other_user.message.text is None
        assert other_user.message.reply_markup is None


async def test_sources_callback_uses_owner_scoped_canonical_order_without_events(
    settings, session_factory
):
    item_id = await _persist_ready_item(session_factory)
    async with session_factory() as session:
        session.add_all(
            [
                ItemSource(
                    item_id=item_id,
                    source_index=1,
                    source_type=SourceType.WEB,
                    source_url="https://example.com/second",
                    extraction_status="READY",
                ),
                ItemSource(
                    item_id=item_id,
                    source_index=0,
                    source_type=SourceType.WEB,
                    source_url="https://example.com/first",
                    extraction_status="READY",
                ),
            ]
        )
        await session.commit()

    callback = FakeCallback(42, f"item:sources:{item_id}")
    await on_item_callback(callback, settings, session_factory)

    buttons = [button for row in callback.message.reply_markup.inline_keyboard for button in row]
    assert [(button.text, button.url) for button in buttons if button.url] == [
        ("↗ Статья — example.com 1", "https://example.com/first"),
        ("↗ Статья — example.com 2", "https://example.com/second"),
    ]
    assert buttons[-1].callback_data == f"item:back:{item_id}"
    assert callback.answers == [None]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 0


async def test_interest_callback_current_level_is_transport_noop(settings, session_factory):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:interest:{item_id}:2")
    callback.message.reply_markup = item_interest_keyboard(_ready_item())

    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.interest_level == 2
        event_count = await session.scalar(
            select(func.count(Event.id)).where(
                Event.item_id == item_id,
                Event.event_type == "INTEREST_CHANGED",
            )
        )
        assert event_count == 0
    assert [row[0].text for row in callback.message.reply_markup.inline_keyboard] == [
        "1 — Низкий",
        "2 — Обычный ✓",
        "3 — Высокий",
        "← Назад",
    ]
    assert callback.answers == ["Уже выбран этот уровень"]


async def test_stale_interest_callback_after_archive_is_rejected_without_event(
    settings, session_factory
):
    item_id = await _persist_ready_item(session_factory)
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        item.state = ItemState.ARCHIVED
        await session.commit()

    for action in ("interest_menu", "interest"):
        suffix = ":3" if action == "interest" else ""
        callback = FakeCallback(42, f"item:{action}:{item_id}{suffix}")
        await on_item_callback(callback, settings, session_factory)
        assert callback.answers == ["Item недоступен"]

    async with session_factory() as session:
        item = await session.get(Item, item_id)
        events = await session.scalar(
            select(func.count(Event.id)).where(
                Event.item_id == item_id,
                Event.event_type == "INTEREST_CHANGED",
            )
        )
    assert item.interest_level == 2
    assert events == 0

    stale_more = FakeCallback(42, f"item:more:{item_id}")
    await on_item_callback(stale_more, settings, session_factory)
    stale_callbacks = _callback_data(stale_more.message.reply_markup)
    assert f"item:interest_menu:{item_id}" not in stale_callbacks
    assert not {
        f"item:done:{item_id}",
        f"item:later:{item_id}",
        f"item:archive:{item_id}",
    } & set(stale_callbacks)


async def test_malformed_interest_callback_does_not_mutate(settings, session_factory):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:interest:{item_id}:4")

    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.interest_level == 2
    assert callback.message.text is None
    assert callback.answers == ["Некорректный уровень интереса"]


async def test_other_allowed_user_cannot_change_interest(settings, session_factory):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(1000, f"item:interest:{item_id}:3")

    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.scalar(select(Item).where(Item.id == item_id))
        assert stored.interest_level == 2
    assert callback.message.text is None
    assert callback.answers == ["Item недоступен"]
