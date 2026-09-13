"""Phase 7: visual analysis — frames extraction, vision enrichment, graceful degradation."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import ContentKind, ProcessingStatus
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.youtube import YoutubeExtractor
from app.services.analysis import Analyzer
from app.services.frames import extract_representative_frames
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeLlmProvider, FakeTranscriber

URL = "https://www.youtube.com/watch?v=abc123"

SUBTITLE_VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Архитектура агентов: оркестрация.

00:00:04.000 --> 00:00:08.000
Планировщик, инструменты, критик.
"""


def make_info(**overrides) -> dict:
    base = {
        "id": "abc123",
        "title": "Архитектура AI-агентов",
        "description": "Видео про оркестраторы агентов.",
        "duration": 600,
        "webpage_url": URL,
        "subtitles": {
            "ru": [{"ext": "vtt", "url": "https://sub.example.com/ru.vtt"}],
        },
        "automatic_captions": {},
    }
    base.update(overrides)
    return base


def make_visual_worker(session_factory, tmp_path, provider):
    def ydl_factory(options):
        class FakeYdl:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return make_info()

            def prepare_filename(self, info):
                return str(tmp_path / "video.mp4")

        return FakeYdl()

    youtube = YoutubeExtractor(
        transcriber=FakeTranscriber(),
        temp_dir=tmp_path / "yt",
        ydl_factory=ydl_factory,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=SUBTITLE_VTT)),
            follow_redirects=True,
            timeout=5,
        ),
    )

    async def download_video(url, work_dir):
        video = work_dir / "video.mp4"
        video.write_bytes(b"fake-video")
        return video

    youtube.download_video = download_video
    pipeline = ProcessingPipeline(
        Analyzer(provider),
        PriorityEngine(),
        youtube_extractor=youtube,
        visual_frame_interval_seconds=1,
        visual_max_frames=120,
    )
    return ProcessingWorker(session_factory, pipeline, poll_seconds=0.01)


async def seed_youtube(session_factory, message_id=1):
    return (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=message_id,
            text=f"Видео про агентов {URL}",
        )
    ).items[0]


async def get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


def patch_frames(monkeypatch, captured):
    """Подмена frame extraction: 3 уникальных кадра в work_dir."""

    def fake_extract(video, work_dir, **kwargs):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(3):
            frame = work_dir / f"frame_{i:04d}.jpg"
            frame.write_bytes(f"frame-{i}".encode())
            frames.append(frame)
        captured.append(list(frames))
        return frames

    monkeypatch.setattr("app.services.processing.extract_representative_frames", fake_extract)


async def test_visual_notes_persisted_and_completeness(tmp_path, session_factory, monkeypatch):
    item = await seed_youtube(session_factory)
    provider = FakeLlmProvider(vision=True)
    worker = make_visual_worker(session_factory, tmp_path, provider)
    captured: list = []
    patch_frames(monkeypatch, captured)

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.analysis_completeness == "TRANSCRIPT_AND_VISUAL"
    async with session_factory() as session:
        notes = (
            await session.scalars(
                select(Content).where(
                    Content.item_id == item.id, Content.kind == ContentKind.VISUAL_NOTES
                )
            )
        ).all()
    assert len(notes) == 1
    assert captured and len(captured[0]) == 3


async def test_no_vision_capability_transcript_only(tmp_path, session_factory):
    item = await seed_youtube(session_factory)
    provider = FakeLlmProvider(vision=False)
    worker = make_visual_worker(session_factory, tmp_path, provider)

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.analysis_completeness == "TRANSCRIPT_ONLY"
    async with session_factory() as session:
        notes = (
            await session.scalars(
                select(Content).where(
                    Content.item_id == item.id, Content.kind == ContentKind.VISUAL_NOTES
                )
            )
        ).all()
    assert notes == []
    assert provider.describe_calls == 0


async def test_vision_failure_keeps_item_ready_transcript_only(
    tmp_path, session_factory, monkeypatch
):
    item = await seed_youtube(session_factory)
    provider = FakeLlmProvider(vision=True, describe_fail=True)
    worker = make_visual_worker(session_factory, tmp_path, provider)
    patch_frames(monkeypatch, [])

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.analysis_completeness == "TRANSCRIPT_ONLY"


def test_frames_extraction_dedups_identical_frames(tmp_path):
    # Дедупликация: идентичные кадры (статичный слайд) не дублируются.
    def runner(argv):
        pattern = Path(argv[-1]).parent
        (pattern / "frame_0001.jpg").write_bytes(b"same")
        (pattern / "frame_0002.jpg").write_bytes(b"same")
        (pattern / "frame_0003.jpg").write_bytes(b"other")
        return 0

    frames = extract_representative_frames(
        tmp_path / "video.mp4",
        tmp_path / "frames",
        interval_seconds=20,
        max_frames=120,
        runner=runner,
    )
    assert [f.name for f in frames] == ["frame_0001.jpg", "frame_0003.jpg"]


def test_frames_extraction_ffmpeg_failure_raises(tmp_path):
    with pytest.raises(AppError) as exc_info:
        extract_representative_frames(
            tmp_path / "video.mp4",
            tmp_path / "f2",
            interval_seconds=20,
            max_frames=120,
            runner=lambda argv: 1,
        )
    assert exc_info.value.code == "VISUAL_FAILED"
