from datetime import UTC, datetime

from aiogram.types import Chat, Document, Message, MessageOriginChannel, Video
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.handlers import make_router, on_video
from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.video import VideoExtractor
from app.services.analysis import Analyzer
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item, ItemSource
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeDownloader, FakeLlmProvider, FakeTranscriber


def _video_message(*, caption: str | None = "Комментарий к видео") -> Message:
    """Build a real aiogram video update so router/handler tests cover transport shape."""
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="Owner"),
        caption=caption,
        video=Video(
            file_id="video-1",
            file_unique_id="video-unique",
            width=1280,
            height=720,
            duration=30,
            file_size=100,
        ),
    )


def _forwarded_video_document_message() -> Message:
    """Model Telegram's document-shaped transport for a forwarded MP4 post."""
    return Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="Owner"),
        caption="Текст пересланного поста",
        document=Document(
            file_id="document-video-1",
            file_unique_id="document-video-unique",
            file_name="post-video.mp4",
            mime_type="video/mp4",
            file_size=100,
        ),
        forward_origin=MessageOriginChannel(
            type="channel",
            date=datetime.now(UTC),
            chat=Chat(id=-100123, type="channel", title="Video Channel", username="videochan"),
            message_id=77,
        ),
    )


def _fake_audio_converter(video_path, audio_path) -> None:
    """Test seam for ffmpeg: conversion semantics are tested without a local binary."""
    assert video_path.exists()
    audio_path.write_bytes(b"fake-wav")


async def test_video_handler_creates_one_item_with_caption(settings, session_factory, monkeypatch):
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    message = _video_message()

    await on_video(message, settings, session_factory)

    assert sent == ["Принял видео. Разбираю текст, ссылки и видео…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item is not None
        assert item.source_type is SourceType.VIDEO
        assert item.user_note == "Комментарий к видео"
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert source is not None
        assert source.source_type is SourceType.VIDEO
        assert source.source_file_id == "video-1"
        assert (
            await session.scalar(
                select(Content.text).where(
                    Content.item_id == item.id,
                    Content.kind == ContentKind.USER_TEXT,
                )
            )
            == "Комментарий к видео"
        )


async def test_video_is_matched_by_router(settings, session_factory):
    router = make_router(settings, session_factory)
    message = _video_message()

    matched = None
    for handler in router.message.handlers:
        passes, _ = await handler.check(message)
        if passes:
            matched = handler.callback.__name__
            break

    assert matched == "video"


async def test_forwarded_video_document_routes_to_video_pipeline(
    settings, session_factory, monkeypatch
):
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    router = make_router(settings, session_factory)
    message = _forwarded_video_document_message()

    matched = None
    for handler in router.message.handlers:
        passes, _ = await handler.check(message)
        if passes:
            matched = handler.callback.__name__
            await handler.callback(message)
            break

    assert matched == "document"
    assert sent == ["Принял видео. Разбираю текст, ссылки и видео…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item is not None
        assert item.source_type is SourceType.VIDEO
        assert item.source_metadata_json["forwarded"] is True
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert source is not None
        assert source.source_type is SourceType.VIDEO
        assert source.source_file_id == "document-video-1"
        assert (
            await session.scalar(
                select(Content.text).where(
                    Content.item_id == item.id,
                    Content.kind == ContentKind.USER_TEXT,
                )
            )
            == "Текст пересланного поста"
        )


async def test_video_transcript_and_caption_reach_one_analysis(
    tmp_path, settings, session_factory, monkeypatch
):
    async def fake_answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_video(_video_message(), settings, session_factory)
    provider = FakeLlmProvider()
    extractor = VideoExtractor(
        FakeTranscriber("Транскрипт видео"),
        FakeDownloader(),
        tmp_path / "video",
        audio_converter=_fake_audio_converter,
    )
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            video_extractor=extractor,
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert content.text == "Транскрипт видео"
    assert content.user_note == "Комментарий к видео"
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item.processing_status is ProcessingStatus.READY
        assert item.analysis_completeness == "TRANSCRIPT_ONLY"
        transcript = await session.scalar(
            select(Content).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.TRANSCRIPT,
            )
        )
        assert transcript is not None
        assert transcript.text == "Транскрипт видео"


async def test_video_visual_notes_join_transcript_before_single_analysis(
    tmp_path, settings, session_factory, monkeypatch
):
    async def fake_answer(self, text, **kwargs):
        return None

    def fake_frames(video, work_dir, **kwargs):
        work_dir.mkdir(parents=True, exist_ok=True)
        frame = work_dir / "frame.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr("app.services.processing.extract_representative_frames", fake_frames)
    await on_video(_video_message(), settings, session_factory)
    provider = FakeLlmProvider(vision=True, describe_notes="На экране показана диаграмма")
    extractor = VideoExtractor(
        FakeTranscriber("Транскрипт видео"),
        FakeDownloader(),
        tmp_path / "video",
        audio_converter=_fake_audio_converter,
    )
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            video_extractor=extractor,
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    assert provider.describe_calls == 1
    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert content.text == "Транскрипт видео"
    assert content.metadata["visual_notes"] == "На экране показана диаграмма"
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item.analysis_completeness == "TRANSCRIPT_AND_VISUAL"
        visual = await session.scalar(
            select(Content).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.VISUAL_NOTES,
            )
        )
        assert visual is not None
        assert visual.text == "На экране показана диаграмма"


class FailingVideoExtractor:
    """Source-boundary failure used to prove caption fallback at Item level."""

    temp_dir = "."

    async def extract(self, source, **kwargs) -> NormalizedContent:
        raise AppError("TRANSCRIPTION_FAILED", "video audio unavailable")


async def test_video_failure_with_caption_degrades_to_partial_text_analysis(
    settings, session_factory, monkeypatch
):
    async def fake_answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_video(_video_message(caption="Важное описание видео"), settings, session_factory)
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            video_extractor=FailingVideoExtractor(),
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert content.text == "Важное описание видео"
    assert content.metadata["source_failures"][0]["source_type"] == "VIDEO"
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item.processing_status is ProcessingStatus.READY
        assert item.analysis_completeness == "PARTIAL"


async def test_video_failure_without_caption_fails_item(settings, session_factory, monkeypatch):
    async def fake_answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_video(_video_message(caption=None), settings, session_factory)
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            video_extractor=FailingVideoExtractor(),
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    assert provider.calls == []
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item.processing_status is ProcessingStatus.FAILED
        assert item.error_code == "TRANSCRIPTION_FAILED"
