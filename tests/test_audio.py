from pathlib import Path

from sqlalchemy import select

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.extractors.audio import AudioExtractor
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_voice
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeDownloader, FakeLlmProvider, FakeTranscriber, make_analysis


def make_worker(session_factory, transcriber, downloader, temp: Path):
    pipeline = ProcessingPipeline(
        Analyzer(FakeLlmProvider()),
        PriorityEngine(),
        audio_extractor=AudioExtractor(transcriber, downloader, temp),
    )
    return ProcessingWorker(session_factory, pipeline, poll_seconds=0.01)


async def seed_voice(session_factory, file_id="file-123", message_id=1):
    return (
        await ingest_voice(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=message_id,
            file_id=file_id,
            duration_seconds=15,
            source_type=SourceType.VOICE,
        )
    ).items[0]


async def get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


def temp_files(temp: Path) -> list[Path]:
    return list(Path(temp).glob("*"))


async def test_voice_pipeline_end_to_end(tmp_path, session_factory):
    item = await seed_voice(session_factory)
    transcriber = FakeTranscriber()
    worker = make_worker(session_factory, transcriber, FakeDownloader(), tmp_path / "audio")

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.title == make_analysis().title

    # TRANSCRIPT персистится
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Content).where(
                    Content.item_id == item.id, Content.kind == ContentKind.TRANSCRIPT
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].text == "Голосовая заметка: изучить агентов"

    # временный файл удалён после успеха
    assert temp_files(tmp_path / "audio") == []


async def test_voice_item_reaches_analyzer_with_transcript(tmp_path, session_factory):
    await seed_voice(session_factory)
    transcriber = FakeTranscriber()
    worker = make_worker(session_factory, transcriber, FakeDownloader(), tmp_path / "audio")
    provider = worker.pipeline.analyzer.provider

    await worker.process_one()
    ((content, _, _),) = provider.calls
    assert content.text == "Голосовая заметка: изучить агентов"
    assert content.source_type is SourceType.VOICE


async def test_voice_retry_after_stt_failure_reruns_stt(tmp_path, session_factory):
    # STT упал ДО персистенции TRANSCRIPT — retry честно повторяет download+STT.
    item = await seed_voice(session_factory)
    worker = make_worker(
        session_factory, FakeTranscriber(fail=True), FakeDownloader(), tmp_path / "audio"
    )
    assert await worker.process_one() is True

    failed = await get_item(session_factory, item.id)
    assert failed.processing_status is ProcessingStatus.FAILED
    assert failed.error_code == "TRANSCRIPTION_FAILED"
    assert temp_files(tmp_path / "audio") == []  # temp удалён даже при ошибке

    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.QUEUED
        await session.commit()

    working = FakeTranscriber()
    assert (
        await make_worker(
            session_factory, working, FakeDownloader(), tmp_path / "audio"
        ).process_one()
        is True
    )
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY


async def test_transcript_resume_skips_stt_and_download(tmp_path, session_factory):
    # REGRESSION: TRANSCRIPT персистент + stage ANALYZING → resume без STT/download.
    item = await seed_voice(session_factory)
    transcriber = FakeTranscriber()
    worker = make_worker(session_factory, transcriber, FakeDownloader(), tmp_path / "audio")

    await worker.process_one()  # первый прогон успешен → TRANSCRIPT в БД
    assert transcriber.calls == 1

    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.QUEUED
        row.processing_stage = "ANALYZING"
        await session.commit()

    downloader2 = FakeDownloader()
    transcriber2 = FakeTranscriber()
    assert (
        await make_worker(
            session_factory, transcriber2, downloader2, tmp_path / "audio"
        ).process_one()
        is True
    )
    assert transcriber2.calls == 0  # STT не повторялся
    assert downloader2.calls == 0  # файл не скачивался


async def test_download_failure_marks_item_failed(tmp_path, session_factory):
    item = await seed_voice(session_factory)
    worker = make_worker(session_factory, FakeTranscriber(), FakeDownloader(fail=True), tmp_path)
    assert await worker.process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.error_code == "DOWNLOAD_FAILED"


async def test_empty_transcript_is_transcription_failed(tmp_path, session_factory):
    item = await seed_voice(session_factory)
    worker = make_worker(
        session_factory, FakeTranscriber(transcript=""), FakeDownloader(), tmp_path
    )
    await worker.process_one()
    stored = await get_item(session_factory, item.id)
    assert stored.error_code == "TRANSCRIPTION_FAILED"


async def test_voice_ingestion_is_idempotent(session_factory):
    first = await seed_voice(session_factory, message_id=1)
    second = (
        await ingest_voice(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            file_id="file-123",
            duration_seconds=15,
            source_type=SourceType.VOICE,
        )
    ).items[0]
    assert second.id == first.id
