from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.types import (
    Chat,
    Message,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
)
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from app.bot.formatting import format_ready_item
from app.bot.handlers import (
    make_router,
    on_forwarded_photo,
    on_text,
    on_unsupported_document,
    on_voice_audio,
)
from app.bot.keyboards import item_keyboard
from app.bot.provenance import (
    forward_original_url,
    normalize_forward_origin,
)
from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import DEFAULT_PROFILE, NormalizedContent
from app.domain.priority import PriorityEngine
from app.extractors.audio import AudioExtractor
from app.llm.openai import build_user_message
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_media, ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Event, Item
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeDownloader, FakeLlmProvider, FakeTranscriber, make_analysis

FORWARD_DATE = datetime(2026, 9, 1, 12, 14, tzinfo=UTC)


def _message(
    origin,
    *,
    message_id: int = 1,
    text: str | None = "forwarded",
    caption: str | None = None,
    entities: list[MessageEntity] | None = None,
    caption_entities: list[MessageEntity] | None = None,
    photo: list[PhotoSize] | None = None,
) -> Message:
    """Build real aiogram models so provenance tests cover the production transport boundary."""
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="Owner"),
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        photo=photo,
        forward_origin=origin,
    )


def _channel_origin(*, username: str | None = "androiddev", message_id: int = 8712):
    return MessageOriginChannel(
        type="channel",
        date=FORWARD_DATE,
        chat=Chat(id=-100123, type="channel", title="Android Developers", username=username),
        message_id=message_id,
    )


def _capture_answers(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return sent


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            MessageOriginUser(
                type="user",
                date=FORWARD_DATE,
                sender_user=TgUser(
                    id=777,
                    is_bot=False,
                    first_name="Alice",
                    last_name="Smith",
                    username="alice",
                ),
            ),
            {
                "forwarded": True,
                "forward_origin_type": "user",
                "forward_source_name": "Alice Smith",
                "forward_source_username": "alice",
                "original_sent_at": "2026-09-01T12:14:00Z",
            },
        ),
        (
            MessageOriginHiddenUser(
                type="hidden_user",
                date=FORWARD_DATE,
                sender_user_name="Hidden Author",
            ),
            {
                "forwarded": True,
                "forward_origin_type": "hidden_user",
                "forward_source_name": "Hidden Author",
                "original_sent_at": "2026-09-01T12:14:00Z",
            },
        ),
        (
            MessageOriginChat(
                type="chat",
                date=FORWARD_DATE,
                sender_chat=Chat(
                    id=-200,
                    type="group",
                    title="Architecture Chat",
                    username="arch_chat",
                ),
            ),
            {
                "forwarded": True,
                "forward_origin_type": "chat",
                "forward_source_name": "Architecture Chat",
                "forward_source_username": "arch_chat",
                "original_sent_at": "2026-09-01T12:14:00Z",
            },
        ),
        (
            _channel_origin(),
            {
                "forwarded": True,
                "forward_origin_type": "channel",
                "forward_source_name": "Android Developers",
                "forward_source_username": "androiddev",
                "forward_message_id": 8712,
                "original_sent_at": "2026-09-01T12:14:00Z",
            },
        ),
    ],
)
def test_forward_origin_normalization_keeps_only_supplied_provenance(origin, expected):
    assert normalize_forward_origin(origin) == expected


async def test_forwarded_text_is_source_content_not_user_note_and_replay_is_idempotent(
    settings, session_factory, monkeypatch
):
    sent = _capture_answers(monkeypatch)
    message = _message(_channel_origin(), text="Авторский текст про Android")

    await on_text(message, settings, session_factory)
    await on_text(message, settings, session_factory)

    assert sent == ["Принял. Разбираю…", "Принял. Разбираю…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item is not None
        assert item.source_type is SourceType.TEXT
        assert item.user_note == ""
        assert item.source_metadata_json["forward_origin_type"] == "channel"
        assert item.source_metadata_json["forward_message_id"] == 8712
        contents = (
            await session.scalars(
                select(Content).where(
                    Content.item_id == item.id,
                    Content.kind == ContentKind.USER_TEXT,
                )
            )
        ).all()
        assert [content.text for content in contents] == ["Авторский текст про Android"]
        assert await session.scalar(select(func.count()).select_from(Item)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.event_type == "CREATED")
            )
            == 1
        )


async def test_forwarded_text_pipeline_analyzes_original_text_without_user_intent(session_factory):
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            text="Текст исходного автора",
            source_metadata=normalize_forward_origin(_channel_origin()),
        )
    ).items[0]
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine()),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    ((content, _, _),) = provider.calls
    assert content.text == "Текст исходного автора"
    assert content.user_note is None
    assert content.source_context is None
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.READY
        assert stored.user_note == ""


@pytest.mark.parametrize(
    ("url", "source_type"),
    [
        ("https://example.com/article", SourceType.WEB),
        ("https://www.youtube.com/watch?v=forwarded", SourceType.YOUTUBE),
    ],
)
async def test_forwarded_url_reuses_routing_and_persists_author_context(
    session_factory, url, source_type
):
    metadata = normalize_forward_origin(_channel_origin())
    first = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=1,
        text=f"Комментарий исходного автора {url}",
        source_metadata=metadata,
    )
    item = first.items[0]

    assert item.source_type is source_type
    assert item.user_note == ""
    assert item.source_metadata_json == metadata
    async with session_factory() as session:
        source_text = await session.scalar(
            select(Content.text).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.USER_TEXT,
            )
        )
        assert source_text == f"Комментарий исходного автора {url}"

    second = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=2,
        text=url,
        source_metadata={"forwarded": True, "forward_origin_type": "hidden_user"},
    )
    assert len(second.items) == 1
    assert second.items[0].id != item.id
    assert second.duplicates == []


async def test_forwarded_multi_url_post_stays_one_item_with_full_source_content(
    session_factory,
):
    text = (
        "Подборка инструментов для ревью:\n"
        "https://example.com/first\n"
        "Описание между ссылками\n"
        "https://example.com/second\n"
        "https://example.com/third"
    )
    result = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=1,
        text=text,
        source_metadata=normalize_forward_origin(_channel_origin()),
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.source_type is SourceType.TEXT
    assert item.source_url is None
    assert item.user_note == ""
    async with session_factory() as session:
        source_text = await session.scalar(
            select(Content.text).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.USER_TEXT,
            )
        )
        assert source_text == text

    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine()),
        poll_seconds=0.01,
    )
    assert await worker.process_one() is True
    ((content, _, _),) = provider.calls
    assert content.text == text
    assert content.user_note is None


@pytest.mark.parametrize("source_type", [SourceType.VOICE, SourceType.AUDIO])
async def test_forwarded_audio_paths_keep_caption_as_source_context(
    tmp_path, session_factory, source_type
):
    metadata = normalize_forward_origin(_channel_origin())
    item = (
        await ingest_media(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            file_id="file-123",
            duration_seconds=15,
            source_type=source_type,
            source_metadata=metadata,
            source_text="Комментарий автора к аудио",
        )
    ).items[0]
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            audio_extractor=AudioExtractor(
                FakeTranscriber(),
                FakeDownloader(),
                tmp_path / "audio",
            ),
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    ((content, _, _),) = provider.calls
    assert content.source_type is source_type
    assert content.text == "Голосовая заметка: изучить агентов"
    assert content.user_note is None
    assert content.source_context == "Комментарий автора к аудио"
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.title == make_analysis().title
        assert stored.source_metadata_json == metadata


async def test_forwarded_audio_handler_normalizes_origin(settings, session_factory, monkeypatch):
    sent = _capture_answers(monkeypatch)
    message = _message(_channel_origin(), text=None, caption="Подкаст из канала")
    media = SimpleNamespace(file_size=100, file_id="audio-1", duration=30)

    await on_voice_audio(message, settings, session_factory, media, True, 1_000_000)

    assert sent == ["Принял аудио. Разбираю…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item.source_type is SourceType.AUDIO
        assert item.source_metadata_json["forward_source_name"] == "Android Developers"
        assert (
            await session.scalar(select(Content.text).where(Content.kind == ContentKind.USER_TEXT))
            == "Подкаст из канала"
        )


async def test_forwarded_photo_hidden_caption_link_routes_to_web(
    settings, session_factory, monkeypatch
):
    sent = _capture_answers(monkeypatch)
    caption = "Review-prompts — подсказки по проверке кода с помощью ИИ. GitHub"
    message = _message(
        _channel_origin(),
        text=None,
        caption=caption,
        caption_entities=[
            MessageEntity(
                type="text_link",
                offset=caption.index("GitHub"),
                length=len("GitHub"),
                url="https://github.com/masong/review-prompts",
            )
        ],
        photo=[PhotoSize(file_id="photo", file_unique_id="unique", width=640, height=480)],
    )

    await on_forwarded_photo(message, settings, session_factory)

    assert sent == ["Принял пересланное сообщение. Текст и ссылки разбираю…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item is not None
        assert item.source_type is SourceType.WEB
        assert item.source_url == "https://github.com/masong/review-prompts"
        assert item.user_note == ""
        assert item.source_metadata_json["forward_source_name"] == "Android Developers"
        source_text = await session.scalar(
            select(Content.text).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.USER_TEXT,
            )
        )
        assert source_text == f"{caption}\nhttps://github.com/masong/review-prompts"


async def test_forwarded_photo_is_matched_by_router(settings, session_factory):
    caption = "Review-prompts — GitHub"
    message = _message(
        _channel_origin(),
        text=None,
        caption=caption,
        caption_entities=[
            MessageEntity(
                type="text_link",
                offset=caption.index("GitHub"),
                length=len("GitHub"),
                url="https://github.com/masong/review-prompts",
            )
        ],
        photo=[PhotoSize(file_id="photo", file_unique_id="unique", width=640, height=480)],
    )
    router = make_router(settings, session_factory)

    matched = None
    for handler in router.message.handlers:
        passes, _ = await handler.check(message)
        if passes:
            matched = handler.callback.__name__
            break

    assert matched == "forwarded_photo"


def test_source_context_is_not_rendered_as_user_note_for_llm():
    content = NormalizedContent(
        source_type=SourceType.WEB,
        text="Текст статьи",
        source_context="Комментарий автора поста",
    )

    prompt = build_user_message(content, DEFAULT_PROFILE, [])

    assert "SOURCE CONTEXT (untrusted, part of captured source): Комментарий автора поста" in prompt
    assert "USER NOTE" not in prompt


def test_multi_source_prompt_requires_whole_item_synthesis():
    content = NormalizedContent(
        source_type=SourceType.TEXT,
        text="SOURCE 1 [WEB]\nПервая статья\n\nSOURCE 2 [WEB]\nВторая статья",
        source_context="Пятница. Немного новостей: две ссылки",
        metadata={"source_count": 2, "successful_source_count": 2},
    )

    prompt = build_user_message(content, DEFAULT_PROFILE, [])

    assert "MULTI-SOURCE ITEM: 2 sources" in prompt
    assert "do not omit later sources" in prompt
    assert "SOURCE 1 [WEB]" in prompt
    assert "SOURCE 2 [WEB]" in prompt


def test_partial_source_prompt_names_missing_source_and_forbids_inference():
    content = NormalizedContent(
        source_type=SourceType.WEB,
        text="Текст доступной статьи",
        source_context="Пост с двумя ссылками",
        metadata={
            "source_count": 2,
            "successful_source_count": 1,
            "source_failures": [
                {
                    "source_index": 1,
                    "source_type": "WEB",
                    "error_code": "EXTRACTION_FAILED",
                    "error_message": "unavailable",
                }
            ],
        },
    )

    prompt = build_user_message(content, DEFAULT_PROFILE, [])

    assert "TOTAL SOURCES: 2" in prompt
    assert "SUCCESSFULLY EXTRACTED: 1" in prompt
    assert "FAILED SOURCE: index=1 type=WEB reason=EXTRACTION_FAILED" in prompt
    assert "Do not infer or invent them" in prompt
    assert "only successfully extracted sources" in prompt


def test_forwarded_ready_item_shows_source_and_public_original_link_only_when_complete():
    metadata = normalize_forward_origin(_channel_origin())
    item = Item(
        id=1,
        user_id=1,
        processing_status=ProcessingStatus.READY,
        source_type=SourceType.TEXT,
        user_note="",
        title="Forwarded",
        category="AI",
        item_type=make_analysis().item_type,
        priority_score=70,
        source_metadata_json=metadata,
    )

    assert "Источник: Android Developers" in format_ready_item(item)
    buttons = [button for row in item_keyboard(item).inline_keyboard for button in row]
    assert any(
        button.text == "↗ Открыть оригинал" and button.url == "https://t.me/androiddev/8712"
        for button in buttons
    )

    incomplete = dict(metadata)
    incomplete.pop("forward_source_username")
    assert forward_original_url(incomplete) is None
    item.source_metadata_json = incomplete
    buttons = [button for row in item_keyboard(item).inline_keyboard for button in row]
    assert all(button.text != "↗ Открыть оригинал" for button in buttons)


async def test_unsupported_forwarded_media_fails_gracefully(settings, monkeypatch):
    sent = _capture_answers(monkeypatch)
    await on_unsupported_document(_message(_channel_origin(), text=None), settings)
    assert sent == ["Этот формат документа пока не поддерживается."]


def test_forwarding_is_not_a_source_type():
    assert "FORWARDED" not in SourceType.__members__
