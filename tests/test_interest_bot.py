from types import SimpleNamespace

from sqlalchemy import func, select

from app.bot.handlers import on_item_callback
from app.bot.keyboards import item_keyboard
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.storage.models import Event, Item, ItemSource, User


class FakeCallbackMessage:
    """Capture Telegram edits so callback behavior can be verified offline."""

    def __init__(self):
        self.text = None
        self.reply_markup = None

    async def edit_text(self, text, reply_markup=None):
        self.text = text
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


def test_ready_keyboard_defaults_to_interest_two():
    markup = item_keyboard(_ready_item())
    interest_row = markup.inline_keyboard[0]
    assert [button.text for button in interest_row] == ["1", "2 ✓", "3"]
    assert [button.callback_data for button in interest_row] == [
        "item:interest:7:1",
        "item:interest:7:2",
        "item:interest:7:3",
    ]


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
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]

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

    assert ("🔗 Открыть 1", "https://example.com/first") in links
    assert ("🔗 Открыть 2", "https://example.com/second") in links


async def test_interest_callback_updates_persisted_state_and_existing_message(
    settings, session_factory
):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:interest:{item_id}:3")

    await on_item_callback(callback, settings, session_factory)

    async with session_factory() as session:
        stored = await session.get(Item, item_id)
        assert stored.interest_level == 3
    assert "Интерес: 3/3" in callback.message.text
    assert [button.text for button in callback.message.reply_markup.inline_keyboard[0]] == [
        "1",
        "2",
        "3 ✓",
    ]
    assert callback.answers == [None]


async def test_interest_callback_current_level_is_transport_noop(settings, session_factory):
    item_id = await _persist_ready_item(session_factory)
    callback = FakeCallback(42, f"item:interest:{item_id}:2")

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
    assert callback.message.text is None
    assert callback.message.reply_markup is None
    assert callback.answers == [None]


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
    assert callback.answers == ["Item не найден"]
