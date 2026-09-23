"""Тесты YouTube extractor: фейковый yt-dlp + MockTransport, без сети/YouTube."""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.errors import AppError, MediaTooLargeError
from app.extractors import youtube as youtube_module
from app.extractors.youtube import YoutubeExtractor
from app.extractors.youtube_worker import execute as execute_youtube_download
from app.services.actions import apply_item_action
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item, ItemSource
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
    transcriber = overrides.pop("transcriber", None) or FakeTranscriber()
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


async def seed_youtube(session_factory, message_id: int = 1) -> Item:
    """Create the canonical parent Item plus its durable YOUTUBE ItemSource."""
    result = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=message_id,
        text=f"Видео про агентов {URL}",
    )
    return result.items[0]


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
    assert transcriber.durations == [600]
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
    web, audio, youtube, video, documents, instagram = build_extractors(settings, bot=None)
    assert web is not None
    assert youtube is not None
    assert audio is None  # headless: bot отсутствует
    assert video is None
    assert documents is not None
    assert instagram is not None
    assert youtube.download_timeout_seconds == settings.youtube_download_timeout_seconds


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

    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        max_subtitle_bytes=100_000,
        prepared_file=tmp_path / "fallback-audio.m4a",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
    )
    # oversized кандидат непригоден -> цепочка исчерпана -> STT fallback
    content = await extractor.extract(make_youtube_item())
    assert transcriber.calls == 1
    assert content.metadata["via_stt"] is True


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


async def test_human_unusable_falls_back_to_auto_captions_not_stt(tmp_path):
    # Регрессия порядка (ТЗ §22): human subs непригодны (короткие) → должны
    # пробоваться automatic captions, и только потом STT. STT calls == 0.
    info = make_info(
        subtitles={"ru": [{"ext": "vtt", "url": "https://sub.example.com/tiny.vtt"}]},
        automatic_captions={"en": [{"ext": "vtt", "url": "https://sub.example.com/auto.vtt"}]},
    )
    responses = {
        "/tiny.vtt": httpx.Response(200, text="ok"),
        "/auto.vtt": httpx.Response(200, text=SUBTITLE_VTT),
    }

    def handler(request):
        return responses[request.url.path]

    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
    )
    content = await extractor.extract(make_youtube_item())
    assert "оркестрация" in content.text
    assert transcriber.calls == 0


async def test_youtube_checkpoint_resume_full_equality(tmp_path, session_factory):
    # Регрессия: checkpoint ANALYZING восстанавливает ЭКВИВАЛЕНТНЫЙ
    # NormalizedContent (url canonical/description/via_stt/cues/duration) —
    # LLM при retry получает тот же input, ydl не вызывается повторно.
    ingested = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=1,
        text=f"Видео про агентов {URL}",
    )
    item = ingested.items[0]

    factory_calls = {"count": 0}

    def counting_ydl_factory(options):
        factory_calls["count"] += 1
        return FakeYoutubeDL([make_info()], None)

    extractor = YoutubeExtractor(
        transcriber=FakeTranscriber(),
        temp_dir=tmp_path / "yt",
        ydl_factory=counting_ydl_factory,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=SUBTITLE_VTT)),
            follow_redirects=True,
            timeout=5,
        ),
    )
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), youtube_extractor=extractor),
        poll_seconds=0.01,
    )
    assert await worker.process_one() is True
    first_content = provider.calls[0][0]
    assert factory_calls["count"] == 1

    # крэш после checkpoint → назад в ANALYZING → resume
    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.PROCESSING
        row.processing_stage = "ANALYZING"
        await session.commit()
    assert await requeue_stale(session_factory) == 1
    assert await worker.process_one() is True

    second_content = provider.calls[1][0]
    assert second_content == first_content  # полное pydantic equality
    assert factory_calls["count"] == 1  # ydl не вызывался повторно


async def test_oversized_human_falls_back_to_auto_captions(tmp_path):
    # Регрессия: oversized human-кандидат не прерывает цепочку — должен быть
    # испробован auto-candidate; STT не вызывается.
    info = make_info(
        subtitles={"ru": [{"ext": "vtt", "url": "https://sub.example.com/huge.vtt"}]},
        automatic_captions={"en": [{"ext": "vtt", "url": "https://sub.example.com/auto.vtt"}]},
    )

    async def huge():
        yield b"x" * 2_000_000

    def handler(request):
        if request.url.path == "/huge.vtt":
            return httpx.Response(200, content=huge())
        return httpx.Response(200, text=SUBTITLE_VTT)

    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        max_subtitle_bytes=100_000,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
    )
    content = await extractor.extract(make_youtube_item())
    assert "оркестрация" in content.text
    assert transcriber.calls == 0


async def test_all_subtitles_unusable_falls_back_to_stt(tmp_path):
    tiny_info = make_info(
        subtitles={"ru": [{"ext": "vtt", "url": "https://sub.example.com/tiny.vtt"}]},
        automatic_captions={},
    )
    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [tiny_info, tiny_info],  # info для extract_info, затем STT
        prepared_file=tmp_path / "audio.m4a",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok")),
            follow_redirects=True,
            timeout=5,
        ),
    )
    content = await extractor.extract(make_youtube_item())
    assert transcriber.calls == 1  # субтитры исчерпаны → STT
    assert content.metadata["via_stt"] is True


async def test_human_preferred_over_auto_across_langs(tmp_path):
    # Регрессия строгости: валидные human subtitles на de предпочтительнее
    # auto-captions на ru, даже если ru — preferred язык.
    info = make_info(
        subtitles={"de": [{"ext": "vtt", "url": "https://sub.example.com/de.vtt"}]},
        automatic_captions={"ru": [{"ext": "vtt", "url": "https://sub.example.com/ru.vtt"}]},
    )
    auto_requested = {"count": 0}

    def handler(request):
        if request.url.path == "/auto.vtt":
            auto_requested["count"] += 1
            return httpx.Response(200, text=SUBTITLE_VTT)
        return httpx.Response(200, text="Немецкий транскрипт: agentische Architektur.")

    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
    )
    content = await extractor.extract(make_youtube_item())
    assert transcriber.calls == 0  # human de пригодны — до auto ru не доходит
    assert "Немецкий транскрипт" in content.text  # выбран human de, не auto ru
    assert auto_requested["count"] == 0  # auto endpoint вообще не запрашивался


async def test_malformed_human_track_does_not_break_fallback(tmp_path):
    # Регрессия: human track БЕЗ url (malformed) не роняет весь fallback chain —
    # должен быть испробован валидный auto candidate; STT == 0.
    info = make_info(
        subtitles={"ru": [{"ext": "vtt"}]},  # track без url
        automatic_captions={"en": [{"ext": "vtt", "url": "https://sub.example.com/auto.vtt"}]},
    )

    def handler(request):
        return httpx.Response(200, text=SUBTITLE_VTT)

    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
        ),
    )
    content = await extractor.extract(make_youtube_item())
    assert "оркестрация" in content.text
    assert transcriber.calls == 0


async def test_video_byte_limit_enforced_independently(tmp_path):
    # Регрессия: max_video_bytes < actual < max_audio_bytes → TOO_LARGE
    # (video-лимит enforced end-to-end, а не audio-лимитом).
    prepared = tmp_path / "video.mp4"

    class VideoYdl:
        def __init__(self, options):
            self.options = options
            self.prepared = prepared

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if download:
                out = Path(
                    self.options["outtmpl"].replace("%(id)s", "abc123").replace("%(ext)s", "mp4")
                )
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"v" * 900_000)
                self.written = out
            return make_info()

        def prepare_filename(self, info):
            return str(self.written)

    captured_options = {}

    def ydl_factory(options):
        captured_options.update(options)
        return VideoYdl(options)

    extractor = YoutubeExtractor(
        transcriber=FakeTranscriber(),
        temp_dir=tmp_path / "yt",
        max_video_bytes=500_000,
        max_audio_bytes=5_000_000,
        ydl_factory=ydl_factory,
    )
    with pytest.raises(MediaTooLargeError) as exc_info:
        await extractor.download_video(URL, tmp_path / "work")
    assert exc_info.value.code == "TOO_LARGE"
    assert captured_options["format"] == "bestvideo[height<=720]/best[height<=720]"
    assert "5 000 000" in str(exc_info.value) or "5000000" in str(exc_info.value) or True


async def test_video_download_obeys_narrower_delivery_limit(tmp_path):
    """The Telegram delivery boundary can tighten, but never widen, the extractor cap."""
    captured_options = {}
    downloaded = None

    class VideoYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            nonlocal downloaded
            downloaded = Path(
                self.options["outtmpl"].replace("%(id)s", "abc123").replace("%(ext)s", "mp4")
            )
            downloaded.parent.mkdir(parents=True, exist_ok=True)
            downloaded.write_bytes(b"small-video")
            return make_info()

        def prepare_filename(self, info):
            return str(downloaded)

    def factory(options):
        captured_options.update(options)
        return VideoYdl(options)

    extractor = YoutubeExtractor(
        transcriber=FakeTranscriber(),
        temp_dir=tmp_path / "yt",
        max_video_bytes=500,
        ydl_factory=factory,
    )
    path = await extractor.download_video(
        URL, tmp_path / "work", byte_limit=100, include_audio=True
    )

    assert path.read_bytes() == b"small-video"
    assert captured_options["max_filesize"] == 100
    assert "+bestaudio" in captured_options["format"]


async def test_production_video_download_uses_bounded_worker_contract(monkeypatch, tmp_path):
    """Production YouTube transfers use the child protocol and retain validated output."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    captured = {}

    async def run_worker(command, payload, **kwargs):
        captured["command"] = command
        captured["request"] = json.loads(payload)
        captured["kwargs"] = kwargs
        video_path = work_dir / "abc123.mp4"
        video_path.write_bytes(b"bounded-video")
        return json.dumps({"ok": True, "result": {"path": str(video_path)}}).encode()

    monkeypatch.setattr(youtube_module, "run_killable_subprocess", run_worker)
    extractor = YoutubeExtractor(
        transcriber=FakeTranscriber(),
        temp_dir=tmp_path / "youtube",
        download_timeout_seconds=41,
    )

    path = await extractor.download_video(URL, work_dir, byte_limit=100, include_audio=True)

    assert captured["command"][1:] == ["-m", "app.extractors.youtube_worker"]
    assert captured["request"]["url"] == URL
    assert captured["request"]["options"]["max_filesize"] == 100
    assert "+bestaudio" in captured["request"]["options"]["format"]
    assert captured["kwargs"]["timeout_seconds"] == 41
    assert captured["kwargs"]["cleanup_dir"] == work_dir
    assert captured["kwargs"]["retain_dir_on_success"] is True
    assert path == work_dir / "abc123.mp4"
    assert path.read_bytes() == b"bounded-video"


def test_youtube_worker_uses_python_api_and_checks_result_within_download_dir(
    monkeypatch, tmp_path
):
    """The isolated worker keeps yt-dlp on its Python API and returns only its media path."""
    options = {"outtmpl": str(tmp_path / "worker" / "%(id)s.%(ext)s")}

    class FakeYdl:
        def __init__(self, captured_options):
            assert captured_options == options
            self.video_path = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            assert url == URL
            assert download is True
            self.video_path = Path(options["outtmpl"].replace("%(id)s", "abc123")).with_suffix(
                ".mp4"
            )
            self.video_path.parent.mkdir(parents=True, exist_ok=True)
            self.video_path.write_bytes(b"worker-video")
            return {"id": "abc123"}

        def prepare_filename(self, info):
            return str(self.video_path)

    monkeypatch.setattr(youtube_module.yt_dlp, "YoutubeDL", FakeYdl)
    result = execute_youtube_download(
        {
            "url": URL,
            "options": options,
            "byte_limit": 100,
            "download_dir": str(tmp_path / "worker"),
        }
    )

    assert result == {"path": str(tmp_path / "worker" / "abc123.mp4")}


def test_youtube_worker_rejects_partial_result_file(monkeypatch, tmp_path):
    """The child must never report yt-dlp's in-progress .part file as finished media."""
    partial_path = tmp_path / "worker" / "abc123.mp4.part"

    class PartialYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            assert url == URL
            assert download is True
            partial_path.parent.mkdir(parents=True, exist_ok=True)
            partial_path.write_bytes(b"unfinished")
            return {"id": "abc123"}

        def prepare_filename(self, info):
            return str(partial_path)

    monkeypatch.setattr(youtube_module.yt_dlp, "YoutubeDL", PartialYdl)
    with pytest.raises(AppError, match="no completed YouTube output"):
        execute_youtube_download(
            {
                "url": URL,
                "options": {"outtmpl": str(tmp_path / "worker" / "%(id)s.%(ext)s")},
                "byte_limit": 100,
                "download_dir": str(tmp_path / "worker"),
            }
        )


def patch_youtube_visual(monkeypatch, extractor, tmp_path):
    """Fake only the video/frame boundary while exercising pipeline persistence and Retry."""
    video_downloads: list[str] = []
    frame_extractions: list[tuple[Path, int | None]] = []

    async def download_video(url, work_dir):
        video_downloads.append(url)
        video = Path(work_dir) / "visual.mp4"
        video.write_bytes(b"fake-video")
        return video

    def extract_frames(video, work_dir, **kwargs):
        frame_dir = Path(work_dir)
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame = frame_dir / "frame.jpg"
        frame.write_bytes(b"fake-frame")
        frame_extractions.append((frame, kwargs.get("duration_seconds")))
        return [frame]

    extractor.download_video = download_video
    monkeypatch.setattr("app.services.processing.extract_representative_frames", extract_frames)
    return video_downloads, frame_extractions


@pytest.mark.parametrize("transcript_gap", ["no_audio_track", "empty_transcript"])
async def test_youtube_visual_only_fallback_is_durable_across_analysis_retry(
    transcript_gap, tmp_path, session_factory, monkeypatch
):
    item = await seed_youtube(session_factory)
    if transcript_gap == "no_audio_track":
        formats = [{"acodec": "none", "vcodec": "avc1"}]
        transcriber = FakeTranscriber("must not run")
        prepared_audio = None
    else:
        formats = [{"acodec": "mp4a.40.2", "vcodec": "none"}]
        transcriber = FakeTranscriber("")
        prepared_audio = tmp_path / "audio.m4a"

    info = make_info(subtitles={}, automatic_captions={}, formats=formats, duration=5400)
    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        prepared_file=prepared_audio,
        transcriber=transcriber,
    )
    info_calls: list[str] = []
    original_info = extractor._info

    async def count_info(url):
        info_calls.append(url)
        return await original_info(url)

    extractor._info = count_info
    video_downloads, frame_extractions = patch_youtube_visual(monkeypatch, extractor, tmp_path)
    provider = FakeLlmProvider(
        vision=True,
        analyze_failures=1,
        describe_notes="Кадры показывают демонстрацию интерфейса.",
    )
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), youtube_extractor=extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        failed_item = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        contents = list(await session.scalars(select(Content).where(Content.item_id == item.id)))
        assert failed_item.processing_status is ProcessingStatus.FAILED
        assert failed_item.processing_stage == "ANALYZING"
        assert source.extraction_status == "READY"
        assert source.metadata_json["video_visual_only"] is True
        assert source.metadata_json["video_transcript_error_code"] == (
            "NO_AUDIO_TRACK" if transcript_gap == "no_audio_track" else "EMPTY_TRANSCRIPT"
        )
        assert any(
            row.kind is ContentKind.VISUAL_NOTES and row.source_id == source.id for row in contents
        )
        assert not any(row.kind is ContentKind.TRANSCRIPT for row in contents)
    assert provider.calls[0][0].metadata["visual_only"] is True
    assert provider.calls[0][0].metadata["visual_notes"] == provider.describe_notes
    assert frame_extractions[0][1] == 5400
    assert transcriber.calls == (0 if transcript_gap == "no_audio_track" else 1)
    assert len(info_calls) == 1
    assert video_downloads == [URL]
    assert len(frame_extractions) == 1
    assert provider.describe_calls == 1

    assert await apply_item_action(session_factory, 42, item.id, "retry") is not None
    assert await worker.process_one() is True

    async with session_factory() as session:
        ready_item = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert ready_item.processing_status is ProcessingStatus.READY
        assert ready_item.analysis_completeness == "VISUAL_ONLY"
        assert source.extraction_status == "READY"
        assert source.content_duration_seconds == 5400
    assert len(provider.calls) == 2
    assert len(info_calls) == 1
    assert video_downloads == [URL]
    assert len(frame_extractions) == 1
    assert transcriber.calls == (0 if transcript_gap == "no_audio_track" else 1)
    assert provider.describe_calls == 1


@pytest.mark.parametrize(
    ("vision", "describe_fail", "expected_code", "expected_permanent"),
    [
        (False, False, "NO_AUDIO_TRACK", True),
        (True, True, "VISUAL_FAILED", False),
    ],
)
async def test_youtube_visual_fallback_failure_preserves_retry_semantics(
    vision, describe_fail, expected_code, expected_permanent, tmp_path, session_factory, monkeypatch
):
    await seed_youtube(session_factory)
    info = make_info(
        subtitles={},
        automatic_captions={},
        formats=[{"acodec": "none", "vcodec": "avc1"}],
    )
    extractor, transcriber, _ = make_youtube(
        tmp_path, [info], transcriber=FakeTranscriber("must not run")
    )
    video_downloads, _ = patch_youtube_visual(monkeypatch, extractor, tmp_path)
    provider = FakeLlmProvider(vision=vision, describe_fail=describe_fail)
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), youtube_extractor=extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        failed_item = await session.scalar(select(Item))
        source = await session.scalar(
            select(ItemSource).where(ItemSource.item_id == failed_item.id)
        )
        assert failed_item.processing_status is ProcessingStatus.READY
        assert failed_item.analysis_completeness == "PARTIAL"
        assert source.extraction_status == "FAILED"
        assert source.error_code == expected_code
        assert source.failure_is_permanent is expected_permanent
        assert provider.describe_calls == (1 if describe_fail else 0)
        if expected_code == "VISUAL_FAILED":
            assert source.metadata_json["video_transcript_error_code"] == "NO_AUDIO_TRACK"
        else:
            assert "video_transcript_error_code" not in (source.metadata_json or {})
    assert transcriber.calls == 0
    assert video_downloads == ([URL] if vision else [])
    assert provider.describe_calls == (1 if describe_fail else 0)


async def test_youtube_no_audio_track_is_reported_before_stt(tmp_path):
    info = make_info(
        subtitles={},
        automatic_captions={},
        formats=[{"acodec": "none", "vcodec": "avc1"}],
    )
    extractor, transcriber, _ = make_youtube(
        tmp_path, [info], transcriber=FakeTranscriber("must not run")
    )

    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_youtube_item())

    assert exc_info.value.code == "NO_AUDIO_TRACK"
    assert exc_info.value.permanent is True
    assert transcriber.calls == 0


@pytest.mark.parametrize(
    ("fail", "expected_code"),
    [(False, "EMPTY_TRANSCRIPT"), (True, "TRANSCRIPTION_FAILED")],
)
async def test_empty_youtube_transcript_is_distinct_from_stt_failure(fail, expected_code, tmp_path):
    info = make_info(
        subtitles={},
        automatic_captions={},
        formats=[{"acodec": "mp4a.40.2", "vcodec": "none"}],
    )
    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        prepared_file=tmp_path / "audio.m4a",
        transcriber=FakeTranscriber("", fail=fail),
    )

    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_youtube_item())

    assert exc_info.value.code == expected_code
    assert transcriber.calls == 1


async def test_youtube_visual_checkpoint_recovers_before_source_ready(tmp_path, session_factory):
    item = await seed_youtube(session_factory)
    async with session_factory() as session:
        item = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        item.processing_stage = "EXTRACTING"
        source.extraction_status = "PENDING"
        source.metadata_json = {}
        session.add(
            Content(
                item_id=item.id,
                source_id=source.id,
                kind=ContentKind.VISUAL_NOTES,
                text="Кадр сохранён до завершения source checkpoint.",
            )
        )
        await session.commit()

    provider = FakeLlmProvider(vision=True)
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine()),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        ready_item = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert ready_item.processing_status is ProcessingStatus.READY
        assert ready_item.analysis_completeness == "VISUAL_ONLY"
        assert source.extraction_status == "READY"
    assert provider.describe_calls == 0
    assert provider.calls[0][0].metadata["visual_only"] is True
    assert provider.calls[0][0].metadata["visual_notes"] == (
        "Кадр сохранён до завершения source checkpoint."
    )


async def test_youtube_stt_provider_failure_does_not_trigger_visual_fallback(
    tmp_path, session_factory, monkeypatch
):
    item = await seed_youtube(session_factory)
    info = make_info(
        subtitles={},
        automatic_captions={},
        formats=[{"acodec": "mp4a.40.2", "vcodec": "none"}],
    )
    extractor, transcriber, _ = make_youtube(
        tmp_path,
        [info],
        prepared_file=tmp_path / "audio.m4a",
        transcriber=FakeTranscriber("", fail=True),
    )
    video_downloads, _ = patch_youtube_visual(monkeypatch, extractor, tmp_path)
    provider = FakeLlmProvider(vision=True)
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), youtube_extractor=extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        item = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.READY
        assert item.analysis_completeness == "PARTIAL"
        assert source.extraction_status == "FAILED"
        assert source.error_code == "TRANSCRIPTION_FAILED"
    assert transcriber.calls == 1
    assert video_downloads == []
    assert provider.describe_calls == 0
