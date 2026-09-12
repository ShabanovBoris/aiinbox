from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.errors import AppError
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

    provider = worker.pipeline.analyzer.provider
    await worker.process_one()  # первый прогон успешен → TRANSCRIPT в БД
    assert transcriber.calls == 1
    first_content = provider.calls[0][0]

    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.QUEUED
        row.processing_stage = "ANALYZING"
        await session.commit()

    downloader2 = FakeDownloader()
    transcriber2 = FakeTranscriber()
    worker2 = make_worker(session_factory, transcriber2, downloader2, tmp_path / "audio")
    provider2 = worker2.pipeline.analyzer.provider
    assert await worker2.process_one() is True
    assert transcriber2.calls == 0  # STT не повторялся
    assert downloader2.calls == 0  # файл не скачивался
    second_content = provider2.calls[0][0]
    # pydantic equality по всем полям, включая duration_seconds
    assert second_content == first_content


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


def make_real_downloader(bot, tmp_path, **overrides):
    from app.bot.files import TelegramFileDownloader

    defaults = dict(
        max_bytes=1_000_000,
        max_attempts=3,
        backoff_seconds=0.01,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: bot.serve_file(request)),
            follow_redirects=False,
            timeout=5,
        ),
    )
    defaults.update(overrides)
    return TelegramFileDownloader(bot, **defaults)


class FakeTgBot:
    """Минимальный bot double: get_file + token; скачивание через MockTransport."""

    def __init__(self, file_size=None, get_file_fails=False, fail_first_requests=0):
        self.token = "TESTTOKEN"
        self.file_size = file_size
        self.get_file_fails = get_file_fails
        self.get_file_calls = 0
        self.fail_first_requests = fail_first_requests

    async def get_file(self, file_id):
        self.get_file_calls += 1
        if self.get_file_fails:
            raise RuntimeError("network hiccup")
        from types import SimpleNamespace

        return SimpleNamespace(file_size=self.file_size, file_path="voice/file_123.ogg")

    def serve_file(self, request):
        # файл отдаётся в два чанка — проверяет инкрементальный byte-cap;
        # fail_first_requests раз отдаёт 503 (проверка transient retry)

        if request.url.path.endswith("file_123.ogg"):
            if self.fail_first_requests > 0:
                self.fail_first_requests -= 1
                return httpx.Response(503)

            async def chunks():
                yield b"x" * 300_000
                yield b"y" * 300_000

            return httpx.Response(200, content=chunks())
        return httpx.Response(404)


async def test_downloader_transient_503_then_success(tmp_path):
    # Регрессия: первая HTTP-попытка 503 (transient) → retry → success.
    bot = FakeTgBot(fail_first_requests=1)
    downloader = make_real_downloader(bot, tmp_path)
    path = await downloader.download("file-123", tmp_path)
    assert path.exists() and path.stat().st_size > 0
    path.unlink()


async def test_downloader_oversized_streaming_without_file_size(tmp_path):
    # Регрессия: file_size отсутствует — инкрементальный cap всё равно режет.
    bot = FakeTgBot(file_size=None)
    downloader = make_real_downloader(bot, tmp_path, max_bytes=500_000, max_attempts=1)
    with pytest.raises(AppError) as exc_info:
        await downloader.download("file-123", tmp_path)
    assert exc_info.value.code == "TOO_LARGE"
    assert exc_info.value.permanent is True
    assert list(tmp_path.glob("*.bin")) == []  # partial удалён


async def test_downloader_permanent_404_single_attempt(tmp_path):
    class NotFoundBot:
        token = "TESTTOKEN"

        async def get_file(self, file_id):
            from types import SimpleNamespace

            return SimpleNamespace(file_size=None, file_path="missing.ogg")

    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        return httpx.Response(404)

    downloader = make_real_downloader(
        NotFoundBot(),
        tmp_path,
        max_attempts=3,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False, timeout=5
        ),
    )
    with pytest.raises(AppError) as exc_info:
        await downloader.download("file-123", tmp_path)
    assert exc_info.value.code == "DOWNLOAD_FAILED"
    assert exc_info.value.permanent is True
    assert attempts["count"] == 1


async def test_downloader_get_file_network_failure_retries(tmp_path):
    bot = FakeTgBot(get_file_fails=True)
    downloader = make_real_downloader(bot, tmp_path)
    with pytest.raises(AppError) as exc_info:
        await downloader.download("file-123", tmp_path)
    assert exc_info.value.code == "DOWNLOAD_FAILED"
    assert bot.get_file_calls == 3  # transient: 3 попытки
    assert list(tmp_path.glob("*.bin")) == []


async def test_downloader_get_file_not_found_is_permanent(tmp_path):
    # Регрессия: TelegramNotFound (4xx-семантика) — ровно одна попытка.
    class NotFoundBot:
        token = "TESTTOKEN"
        get_file_calls = 0

        async def get_file(self, file_id):
            self.get_file_calls += 1
            from aiogram.exceptions import TelegramNotFound

            raise TelegramNotFound(method="get_file", message="file not found")

    bot = NotFoundBot()
    downloader = make_real_downloader(bot, tmp_path, max_attempts=3)
    with pytest.raises(AppError) as exc_info:
        await downloader.download("file-123", tmp_path)
    assert exc_info.value.code == "DOWNLOAD_FAILED"
    assert exc_info.value.permanent is True
    assert bot.get_file_calls == 1


async def test_voice_size_limit_creates_durable_item_atomically(session_factory):
    # Регрессия: oversized media сохраняется атомарно как FAILED/TOO_LARGE
    # (PRODUCT_SPEC §66) БЕЗ промежуточного claimable QUEUED-состояния.
    ingested = await ingest_voice(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=1,
        file_id="big-file",
        duration_seconds=999,
        source_type=SourceType.VOICE,
        too_large=(25_000_000, 20_000_000),
    )
    item = ingested.items[0]
    async with session_factory() as session:
        row = await session.get(Item, item.id)
        assert row.processing_status is ProcessingStatus.FAILED
        assert row.error_code == "TOO_LARGE"
        assert row.source_file_id == "big-file"
        assert row.content_duration_seconds == 999
        # атомарность: никакой транзакции, в которой Item был бы QUEUED, не было


async def test_stt_timeout_maps_to_timeout_code(tmp_path):
    # Регрессия: configured timeout от STT SDK должен давать TIMEOUT,
    # а не общий TRANSCRIPTION_FAILED.
    import httpx
    from openai import APITimeoutError

    from app.llm.transcription import OpenAiTranscriptionProvider

    provider = OpenAiTranscriptionProvider(api_key="k", model="whisper-test")

    class TimeoutAudio:
        @staticmethod
        async def create(**kwargs):
            raise APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))

    class FakeClient:
        audio = type("audio", (), {"transcriptions": TimeoutAudio})()

    provider._client = FakeClient()
    audio_file = tmp_path / "a.ogg"
    audio_file.write_bytes(b"x")
    with pytest.raises(AppError) as exc_info:
        await provider.transcribe(audio_file)
    assert exc_info.value.code == "TIMEOUT"
