from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Document, Message, MessageOriginChannel, Video
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.formatting import format_ready_item
from app.bot.handlers import make_router, on_video
from app.bot.keyboards import item_keyboard
from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.video import VideoExtractor, _extract_audio_track
from app.services.actions import apply_item_action
from app.services.analysis import Analyzer
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item, ItemSource, User
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


def test_ffmpeg_missing_audio_stream_has_specific_permanent_error(tmp_path, monkeypatch):
    """The no-audio case is a permanent source condition, not a generic ffmpeg failure."""
    monkeypatch.setattr(
        "app.extractors.video.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=234,
            stderr=b"Output file #0 does not contain any stream. Error opening output files.",
        ),
    )

    with pytest.raises(AppError) as error:
        _extract_audio_track(tmp_path / "silent.mp4", tmp_path / "audio.wav")

    assert error.value.code == "NO_AUDIO_TRACK"
    assert error.value.permanent is True


async def test_empty_video_transcript_remains_retryable(tmp_path):
    source = ItemSource(
        item_id=1,
        source_index=0,
        source_type=SourceType.VIDEO,
        source_file_id="video-1",
        content_duration_seconds=30,
    )
    extractor = VideoExtractor(
        FakeTranscriber(""),
        FakeDownloader(),
        tmp_path / "video",
        audio_converter=_fake_audio_converter,
    )

    with pytest.raises(AppError) as error:
        await extractor.extract(source)

    assert error.value.code == "EMPTY_TRANSCRIPT"
    assert error.value.permanent is False


async def test_unknown_document_video_duration_is_probed_before_stt(tmp_path):
    """Unknown Telegram duration cannot bypass the configured media resource cap."""
    source = ItemSource(
        item_id=1,
        source_index=0,
        source_type=SourceType.VIDEO,
        source_file_id="document-video-1",
        content_duration_seconds=None,
    )
    transcriber = FakeTranscriber("must not run")
    converted = False

    def convert(video_path, audio_path):
        nonlocal converted
        converted = True

    extractor = VideoExtractor(
        transcriber,
        FakeDownloader(),
        tmp_path / "video",
        max_duration_seconds=120,
        audio_converter=convert,
        duration_probe=lambda _: 121,
    )

    with pytest.raises(AppError) as error:
        await extractor.extract(source)

    assert error.value.code == "TOO_LARGE"
    assert source.content_duration_seconds == 121
    assert converted is False
    assert transcriber.calls == 0


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
    async with session_factory() as session:
        user = await session.scalar(select(User))
        user.profile_json = {"preferred_language": "ru"}
        await session.commit()
    provider = FakeLlmProvider(vision=True, describe_notes="На экране показана диаграмма")
    extractor = VideoExtractor(
        FakeTranscriber("English video transcript"),
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
    assert provider.describe_languages == ["ru"]
    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert content.text == "English video transcript"
    assert content.metadata["visual_notes"] == "На экране показана диаграмма"
    assert provider.calls[0][1].preferred_language == "ru"
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


async def test_video_without_audio_is_analyzed_from_visual_notes_and_restored(
    tmp_path, settings, session_factory, monkeypatch
):
    """An unmarked visual note checkpoint restores after interruption before source READY."""

    async def fake_answer(self, text, **kwargs):
        return None

    def fake_frames(video, work_dir, **kwargs):
        work_dir.mkdir(parents=True, exist_ok=True)
        frame = work_dir / "frame.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    def no_audio(video_path, audio_path):
        raise AppError("NO_AUDIO_TRACK", "video has no audio track", permanent=True)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr("app.services.processing.extract_representative_frames", fake_frames)
    await on_video(_video_message(caption=None), settings, session_factory)
    provider = FakeLlmProvider(vision=True, describe_notes="На экране текст Works Everywhere")
    downloader = FakeDownloader()
    transcriber = FakeTranscriber("must not run")
    extractor = VideoExtractor(
        transcriber,
        downloader,
        tmp_path / "video",
        audio_converter=no_audio,
    )
    pipeline = ProcessingPipeline(
        Analyzer(provider),
        PriorityEngine(),
        video_extractor=extractor,
    )
    worker = ProcessingWorker(session_factory, pipeline, poll_seconds=0.01)

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    analyzed_content = provider.calls[0][0]
    assert analyzed_content.metadata["visual_only"] is True
    assert analyzed_content.metadata["visual_notes"] == "На экране текст Works Everywhere"
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.READY
        assert item.analysis_completeness == "VISUAL_ONLY"
        assert "⚠️ Анализ только по кадрам — транскрипт недоступен." in format_ready_item(item)
        assert source.extraction_status == "READY"
        assert source.metadata_json["video_visual_only"] is True
        assert (
            await session.scalar(
                select(Content).where(
                    Content.item_id == item.id,
                    Content.source_id == source.id,
                    Content.kind == ContentKind.TRANSCRIPT,
                )
            )
            is None
        )
        source.metadata_json = {
            key: value
            for key, value in source.metadata_json.items()
            if key not in {"video_visual_only", "video_transcript_error_code"}
        }
        source.extraction_status = "PENDING"
        restored = await ProcessingPipeline._restored_source_content(session, item, source)
        assert restored is not None
        assert restored.metadata["visual_only"] is True
        assert restored.metadata["visual_notes"] == "На экране текст Works Everywhere"
        item.processing_status = ProcessingStatus.QUEUED
        item.processing_stage = "EXTRACTING"
        await session.commit()

    assert await worker.process_one() is True
    assert len(provider.calls) == 2
    assert provider.describe_calls == 1
    assert downloader.calls == 2
    assert transcriber.calls == 0
    async with session_factory() as session:
        source = await session.scalar(select(ItemSource))
        assert source.extraction_status == "READY"


async def test_restored_video_checkpoint_is_ready_before_analyzer_retry(
    tmp_path, settings, session_factory, monkeypatch
):
    """Complete recovered source state before an independent analysis can fail."""

    async def fake_answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_video(_video_message(caption=None), settings, session_factory)
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        source.metadata_json = {
            "video_transcript_error_code": "NO_AUDIO_TRACK",
            "failure_permanent": False,
        }
        item.processing_stage = "EXTRACTING"
        session.add(
            Content(
                item_id=item.id,
                source_id=source.id,
                kind=ContentKind.VISUAL_NOTES,
                text="На кадре сохранённый текст",
            )
        )
        await session.commit()

    provider = FakeLlmProvider(vision=True, analyze_failures=1)
    downloader = FakeDownloader()
    transcriber = FakeTranscriber("must not run")
    extractor = VideoExtractor(
        transcriber,
        downloader,
        tmp_path / "video",
        audio_converter=lambda *_: pytest.fail("restored video must not be converted"),
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
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.FAILED
        assert item.processing_stage == "ANALYZING"
        assert source.extraction_status == "READY"
        assert source.error_code is None
        assert source.error_message is None
        assert "failure_permanent" not in source.metadata_json
        assert source.metadata_json["video_transcript_error_code"] == "NO_AUDIO_TRACK"

    assert downloader.calls == 0
    assert transcriber.calls == 0
    assert provider.describe_calls == 0
    assert await apply_item_action(session_factory, 42, item.id, "retry") is not None
    assert await worker.process_one() is True

    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.READY
        assert item.analysis_completeness == "VISUAL_ONLY"
        assert source.extraction_status == "READY"
    assert len(provider.calls) == 2
    assert all(
        call[0].metadata["visual_notes"] == "На кадре сохранённый текст" for call in provider.calls
    )
    assert downloader.calls == 0
    assert transcriber.calls == 0
    assert provider.describe_calls == 0


@pytest.mark.parametrize("failure_stage", ["no_vision", "frame_extraction", "vision_provider"])
async def test_video_visual_fallback_failure_preserves_retry_semantics(
    failure_stage, tmp_path, settings, session_factory, monkeypatch
):
    """Missing vision keeps the permanent audio error; transient vision failures stay retryable."""

    async def fake_answer(self, text, **kwargs):
        return None

    def fake_frames(video, work_dir, **kwargs):
        if failure_stage == "frame_extraction":
            raise AppError("VISUAL_FAILED", "temporary frame extraction failure")
        work_dir.mkdir(parents=True, exist_ok=True)
        frame = work_dir / "frame.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    def no_audio(video_path, audio_path):
        raise AppError("NO_AUDIO_TRACK", "video has no audio track", permanent=True)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr("app.services.processing.extract_representative_frames", fake_frames)
    await on_video(_video_message(caption=None), settings, session_factory)
    provider = FakeLlmProvider(
        vision=failure_stage != "no_vision",
        describe_fail=failure_stage == "vision_provider",
    )
    extractor = VideoExtractor(
        FakeTranscriber("must not run"),
        FakeDownloader(),
        tmp_path / "video",
        audio_converter=no_audio,
    )
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), video_extractor=extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True

    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.FAILED
        assert source.extraction_status == "FAILED"
        retry_actions = [
            button.callback_data
            for row in item_keyboard(item, [source]).inline_keyboard
            for button in row
        ]
        if failure_stage == "no_vision":
            assert item.error_code == "NO_AUDIO_TRACK"
            assert source.error_code == "NO_AUDIO_TRACK"
            assert source.failure_is_permanent is True
            assert f"item:retry:{item.id}" not in retry_actions
            assert provider.describe_calls == 0
        else:
            assert item.error_code == "VISUAL_FAILED"
            assert source.error_code == "VISUAL_FAILED"
            assert source.failure_is_permanent is False
            assert source.metadata_json["video_transcript_error_code"] == "NO_AUDIO_TRACK"
            assert f"item:retry:{item.id}" in retry_actions


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
