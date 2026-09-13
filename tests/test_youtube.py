"""Тесты YouTube extractor: фейковый yt-dlp + MockTransport, без сети/YouTube."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.youtube import YoutubeExtractor
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item
from app.workers.processing import ProcessingWorker, requeue_stale
from tests.fakes import FakeDownloader, FakeLlmProvider, FakeTranscriber

URL = "https://www.youtube.com/watch?v=abc123"


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


class FakeYoutubeDL:
    """Подмена yt_dlp.YoutubeDL: extract_info возвращает фикстуру/кидает."""

    def __init__(self, results: list, prepared_file: Path | None = None):
        self.results = results
        self.prepared_file = prepared_file
        self.download_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    async def _next(self):
        return self.results.pop(0) if self.results else make_info()

    def extract_info(self, url, download=False):
        result = self.results.pop(0) if self.results else make_info()
        if isinstance(result, Exception):
            raise result
        if download:
            self.download_calls += 1
            if self.prepared_file is not None:
                self.prepared_file.write_bytes(b"fake-audio")
        return result

    def prepare_filename(self, info):
        return str(self.prepared_file or "")


def make_youtube(tmp_path: Path, ydl_results: list, sub_http=None, **overrides):
    transcriber = FakeTranscriber()
    downloader = FakeDownloader()
    sub_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=SUBTITLE_VTT)),
        follow_redirects=True,
        timeout=5,
    )
    prepared_file = overrides.pop("prepared_file", None)
    http_client_factory = overrides.pop("http_client_factory", lambda: sub_client)
    extractor = YoutubeExtractor(
        transcriber=transcriber,
        temp_dir=tmp_path / "yt",
        ydl_factory=lambda options: FakeYoutubeDL(ydl_results, prepared_file),
        http_client_factory=http_client_factory,
        **overrides,
    )
    return extractor, transcriber, downloader


SUBTITLE_VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Архитектура агентов: оркестрация.

00:00:04.000 --> 00:00:08.000
Планировщик, инструменты, критик.
"""


def make_youtube_item(source_url: str = URL) -> Item:
    return Item(
        id=1,
        user_id=1,
        source_type=SourceType.YOUTUBE,
        source_url=source_url,
        processing_status=ProcessingStatus.PROCESSING,
        user_note="",
    )


async def test_subtitles_used_without_stt(tmp_path):
    info = make_info()
    extractor, transcriber, _ = make_youtube(tmp_path, [info])
    content = await extractor.extract(make_youtube_item())
    assert "оркестрация" in content.text
    assert transcriber.calls == 0  # STT не нужен: субтитры пригодные
    assert content.title == "Архитектура AI-агентов"
    assert content.duration_seconds == 600
    assert content.metadata["via_stt"] is False


async def test_automatic_captions_used_without_manual(tmp_path):
    info = make_info(
        subtitles={},
        automatic_captions={"en": [{"ext": "vtt", "url": "https://sub.example.com/auto.vtt"}]},
    )
    extractor, transcriber, _ = make_youtube(tmp_path, [info])
    content = await extractor.extract(make_youtube_item())
    assert "оркестрация" in content.text
    assert transcriber.calls == 0


async def test_no_subtitles_falls_back_to_stt(tmp_path):
    info = make_info(subtitles={}, automatic_captions={})
    prepared = tmp_path / "audio.m4a"
    extractor, transcriber, _ = make_youtube(tmp_path, [info], prepared_file=prepared)
    content = await extractor.extract(make_youtube_item())
    assert transcriber.calls == 1
    assert content.metadata["via_stt"] is True
    assert not prepared.exists()  # temp audio удалён


async def test_playlist_url_rejected(tmp_path):
    playlist = {"_type": "playlist", "entries": [make_info(), make_info()]}
    extractor, _, _ = make_youtube(tmp_path, [playlist])
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_youtube_item())
    assert exc_info.value.code == "UNSUPPORTED_SOURCE"


async def test_duration_cap_is_too_large(tmp_path):
    extractor, _, _ = make_youtube(
        tmp_path, [make_info(duration=999_999)], max_duration_seconds=7200
    )
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_youtube_item())
    assert exc_info.value.code == "TOO_LARGE"


async def test_ytdlp_failure_is_download_failed(tmp_path):
    extractor, _, _ = make_youtube(tmp_path, [AppError("DOWNLOAD_FAILED", "boom")])
    # AppError пройдёт сквозь extract_info — оборачиваем через Exception-путь
    with pytest.raises(AppError):
        await extractor.extract(make_youtube_item())


async def test_youtube_pipeline_persists_transcript_and_resumes(tmp_path, session_factory):
    ingested = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=1,
        text=f"Видео про агентов {URL}",
    )
    item = ingested.items[0]
    assert item.source_type is SourceType.YOUTUBE

    extractor, transcriber, _ = make_youtube(tmp_path, [make_info()])
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), youtube_extractor=extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.READY
        rows = (await session.scalars(select(Content).where(Content.item_id == item.id))).all()
    kinds = {row.kind for row in rows}
    assert ContentKind.TRANSCRIPT in kinds
    assert ContentKind.DESCRIPTION in kinds

    # resume из ANALYZING: TRANSCRIPT персистен — extraction не повторяется.
    # Имитируем крэш после checkpoint: назад в PROCESSING/ANALYZING, затем requeue.
    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.PROCESSING
        row.processing_stage = "ANALYZING"
        await session.commit()
    assert await requeue_stale(session_factory) == 1
    assert await worker.process_one() is True
    # точная проверка: youtube ydl и STT не вызывались повторно
    assert transcriber.calls == 0
    assert len(provider.calls) == 2


def test_production_composition_wires_youtube_extractor(tmp_path):
    # Регрессия Phase 6: composition root обязан подключать YoutubeExtractor —
    # без этого все YOUTUBE Items падают с UNSUPPORTED_SOURCE в production.
    from app.config import Settings
    from app.main import build_extractors

    settings = Settings(
        _env_file=None,
        openai_api_key="k",
        openai_analysis_model="m",
        openai_transcription_model="w",
        temp_dir=str(tmp_path),
    )
    web, audio, youtube = build_extractors(settings, bot=None)
    assert web is not None
    assert youtube is not None
    assert audio is None  # headless: bot отсутствует


def test_www_youtube_nocookie_classified():
    from app.extractors.youtube import is_youtube_url

    assert is_youtube_url("https://www.youtube-nocookie.com/embed/abc")
    assert is_youtube_url("https://youtube-nocookie.com/embed/abc")
    assert not is_youtube_url("https://example.com/watch?v=abc")


def test_vtt_kind_language_headers_do_not_leak():
    from app.services.subtitles import parse_subtitles

    vtt = "WEBVTT\nKind: captions\nLanguage: ru\n\n00:00:01.000 --> 00:00:02.000\nтекст\n"
    text, _ = parse_subtitles(vtt)
    assert "Kind:" not in text and "Language:" not in text
    assert "текст" in text


async def test_subtitle_oversize_is_too_large(tmp_path):
    info = make_info(subtitles={"ru": [{"ext": "vtt", "url": "https://sub.example.com/ru.vtt"}]})
    big = "x" * 2_000_000

    async def body():
        yield big.encode()

    def handler(request):
        return httpx.Response(200, content=body())

    extractor, _, _ = make_youtube(
        tmp_path,
        [info],
        max_subtitle_bytes=100_000,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
    )
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_youtube_item())
    assert exc_info.value.code == "TOO_LARGE"


async def test_subtitle_transient_retry_success(tmp_path):
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text=SUBTITLE_VTT)

    info = make_info(subtitles={"ru": [{"ext": "vtt", "url": "https://sub.example.com/ru.vtt"}]})
    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
        backoff_seconds=0.01,
    )
    await extractor.extract(make_youtube_item())
    assert attempts["count"] == 2
    assert transcriber.calls == 0


async def test_ytdlp_partial_files_cleaned_on_failure(tmp_path):
    # Регрессия: если yt-dlp пишет partial-файл и падает — temp-поддиректория
    # экстрактора полностью удаляется.
    class PartialThenFailYdl:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if download:
                partial = Path(
                    self.options["outtmpl"].replace("%(id)s", "abc123").replace("%(ext)s", "part")
                )
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"partial")
            raise AppError("DOWNLOAD_FAILED", "yt-dlp boom", permanent=True)

        def prepare_filename(self, info):
            return "missing"

    extractor, _, _ = make_youtube(
        tmp_path,
        [make_info(subtitles={}, automatic_captions={}), make_info()],
        prepared_file=tmp_path / "audio.m4a",
    )
    extractor._ydl_factory = lambda options: PartialThenFailYdl()

    # подменить options-запись: PartialThenFailYdl читает self.options — зададим атрибут
    class WithOptions(PartialThenFailYdl):
        def __init__(self, options):
            self.options = options

    extractor._ydl_factory = lambda options: WithOptions(options)
    with pytest.raises(AppError):
        await extractor.extract(make_youtube_item())
    # вся temp-поддиректория экстрактора удалена
    assert list((tmp_path / "yt").iterdir()) == []
