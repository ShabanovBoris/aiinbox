"""Offline regressions for Instagram URL routing, extraction, and durable resume."""

import asyncio
import threading
from pathlib import Path

import pytest
import yt_dlp
from sqlalchemy import select

from app.bot.formatting import format_instagram_failure_reason, format_ready_item
from app.bot.notify import send_item_failure
from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import NormalizedContent, UserProfile
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.instagram import InstagramExtractor, is_instagram_reel_url
from app.llm.base import TranscriptionSegmentCheckpoint
from app.llm.openai import build_user_message
from app.services.actions import apply_item_action
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item, ItemSource, User
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeLlmProvider, FakeTranscriber

REEL_A = "https://www.instagram.com/reel/ABC123/"
REEL_B = "https://instagram.com/reel/XYZ789"
SHARED_REEL = "https://www.instagram.com/reel/ABC123/?stkn=example-token%3D%3D"


def reel_info(**overrides) -> dict:
    """Provide one deterministic yt-dlp result without contacting Instagram."""
    info = {
        "id": "ABC123",
        "title": "Короткий разбор",
        "description": "Подробная подпись к ролику, содержащая полезное описание темы.",
        "uploader": "Example Creator",
        "uploader_id": "example_creator",
        "duration": 30,
        "webpage_url": REEL_A,
        "formats": [{"acodec": "mp4a.40.2"}],
    }
    info.update(overrides)
    return info


class FakeInstagramYdl:
    """Emulate metadata and bounded media output at the yt-dlp adapter boundary."""

    def __init__(self, options, info, calls, media_bytes):
        self.options = options
        self.info = info
        self.calls = calls
        self.media_bytes = media_bytes
        self.prepared_path: Path | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        self.calls.append((url, download, dict(self.options)))
        if download:
            for hook in self.options.get("progress_hooks", []):
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": len(self.media_bytes),
                        "total_bytes": len(self.media_bytes),
                    }
                )
            extension = "m4a" if "bestaudio" in self.options["format"] else "mp4"
            output = Path(self.options["outtmpl"].replace("%(ext)s", extension))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self.media_bytes)
            self.prepared_path = output
        return dict(self.info)

    def prepare_filename(self, info):
        return str(self.prepared_path)


def extractor_for(
    tmp_path: Path,
    info: dict | None = None,
    *,
    transcriber=None,
    media_bytes: bytes = b"fake-media",
    duration_probe=None,
    **overrides,
):
    """Wire the real adapter to an in-memory yt-dlp fake and a deterministic STT provider."""
    calls = []
    options_seen = []
    info = info or reel_info()

    def factory(options):
        options_seen.append(dict(options))
        return FakeInstagramYdl(options, info, calls, media_bytes)

    kwargs = {
        "transcriber": transcriber or FakeTranscriber(),
        "temp_dir": tmp_path / "instagram",
        "ydl_factory": factory,
        "backoff_seconds": 0,
    }
    if duration_probe is not None:
        kwargs["duration_probe"] = duration_probe
    kwargs.update(overrides)
    return InstagramExtractor(**kwargs), calls, options_seen


def instagram_item(url: str = REEL_A) -> Item:
    """Create an unpersisted source-shaped Item for extractor contract tests."""
    return Item(
        id=1,
        user_id=1,
        source_type=SourceType.INSTAGRAM,
        source_url=url,
        processing_status=ProcessingStatus.PROCESSING,
        user_note="",
    )


def make_worker(session_factory, extractor, provider):
    """Connect the Instagram adapter to the existing ItemSource analysis pipeline."""
    return ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            instagram_extractor=extractor,
        ),
        poll_seconds=0.01,
    )


async def ingest_reel(session_factory, message_id: int = 1, text: str = REEL_A):
    """Create the normal message-owned Item and its ordered URL child source."""
    return (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=message_id,
            text=text,
        )
    ).items[0]


@pytest.mark.parametrize(
    "url",
    [
        REEL_A,
        REEL_B,
        "https://www.instagram.com/reel/ABC123/?utm_source=telegram&igsh=keep-me",
        SHARED_REEL,
    ],
)
def test_reel_url_recognition_accepts_only_canonical_reel_hosts(url):
    """Do not send profiles, stories, lookalike hosts, or arbitrary subdomains to yt-dlp."""
    assert is_instagram_reel_url(url)
    for unsupported in (
        "https://instagram.com/example_user/",
        "https://instagram.com/example_user/reels/",
        "https://instagram.com/stories/example_user/123/",
        "https://instagram.com.evil.test/reel/ABC123/",
        "https://m.instagram.com/reel/ABC123/",
        "https://example.com/path/instagram/reel/ABC123/",
    ):
        assert not is_instagram_reel_url(unsupported)


async def test_reels_route_to_child_sources_with_message_local_dedup(session_factory):
    """Multiple Reels and WEB links remain child sources of their one Telegram Item."""
    item = await ingest_reel(
        session_factory,
        text=f"Сравнить {REEL_A} {REEL_A}?utm_source=telegram {REEL_B} https://example.com/article",
    )
    async with session_factory() as session:
        sources = (
            await session.scalars(
                select(ItemSource)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        ).all()
    assert item.source_type is SourceType.TEXT
    assert [(source.source_type, source.source_url) for source in sources] == [
        (SourceType.INSTAGRAM, REEL_A),
        (SourceType.INSTAGRAM, REEL_B),
        (SourceType.WEB, "https://example.com/article"),
    ]
    another_message = await ingest_reel(session_factory, message_id=2)
    assert another_message.id != item.id


async def test_metadata_then_stt_uses_existing_transcription_checkpoints(tmp_path):
    """Metadata precedes bounded media download; STT receives durable segment callbacks."""

    class RecordingTranscriber(FakeTranscriber):
        async def transcribe(
            self,
            audio_path,
            *,
            duration_seconds=None,
            completed_segments=None,
            on_segment=None,
        ):
            self.completed_segments = completed_segments
            self.on_segment = on_segment
            return await super().transcribe(
                audio_path,
                duration_seconds=duration_seconds,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )

    transcriber = RecordingTranscriber()
    extractor, calls, options = extractor_for(tmp_path, transcriber=transcriber)
    checkpoint = TranscriptionSegmentCheckpoint("part", "a" * 64, "provider", "model", 300, "v1")
    completed = {0: checkpoint}

    async def on_segment(index, value):
        return None

    source = instagram_item()
    content = await extractor.extract(source, completed_segments=completed, on_segment=on_segment)

    assert content.source_type is SourceType.INSTAGRAM
    assert content.text == transcriber.transcript
    assert content.source_context == reel_info()["description"]
    assert content.metadata["instagram_id"] == "ABC123"
    assert content.metadata["via_stt"] is True
    assert source.content_duration_seconds == 30
    assert transcriber.durations == [30]
    assert transcriber.completed_segments is completed
    assert transcriber.on_segment is on_segment
    assert [call[1] for call in calls] == [False, True]
    assert options[0]["skip_download"] is True
    assert options[1]["noplaylist"] is True
    assert options[1]["quiet"] is True
    assert options[1]["no_warnings"] is True
    assert options[1]["socket_timeout"] > 0
    assert options[1]["max_filesize"] == extractor.max_audio_bytes
    assert Path(options[1]["outtmpl"]).parent.name.startswith("ig-")
    assert list((tmp_path / "instagram").iterdir()) == []


async def test_known_oversized_duration_stops_before_media_or_stt(tmp_path):
    """Known duration is rejected at metadata time before expensive processing."""
    extractor, calls, _ = extractor_for(tmp_path, reel_info(duration=51), max_duration_seconds=50)
    with pytest.raises(AppError) as error:
        await extractor.extract(instagram_item())
    assert error.value.code == "TOO_LARGE"
    assert [call[1] for call in calls] == [False]
    assert extractor.transcriber.calls == 0


async def test_unknown_duration_is_probed_before_stt(tmp_path):
    """Missing metadata cannot bypass the duration cap or reach the STT provider."""
    probes = []
    extractor, calls, _ = extractor_for(
        tmp_path,
        reel_info(duration=None),
        max_duration_seconds=50,
        duration_probe=lambda path: probes.append(path) or 51,
    )
    with pytest.raises(AppError) as error:
        await extractor.extract(instagram_item())
    assert error.value.code == "TOO_LARGE"
    assert len(probes) == 1
    assert [call[1] for call in calls] == [False, True]
    assert extractor.transcriber.calls == 0
    assert list((tmp_path / "instagram").iterdir()) == []


async def test_actual_download_size_is_checked_and_temp_directory_removed(tmp_path):
    """The final file size is checked even when yt-dlp cannot report an estimate."""
    extractor, _, _ = extractor_for(
        tmp_path,
        max_audio_bytes=4,
        media_bytes=b"12345",
    )
    with pytest.raises(AppError) as error:
        await extractor.extract(instagram_item())
    assert error.value.code == "TOO_LARGE"
    assert list((tmp_path / "instagram").iterdir()) == []


async def test_collections_are_rejected_and_auth_retry_is_bounded(tmp_path):
    """Collection results stay unsupported; auth and rate limits map to stable codes."""
    collection, collection_calls, _ = extractor_for(
        tmp_path, {"id": "ABC123", "entries": [reel_info()]}
    )
    with pytest.raises(AppError) as collection_error:
        await collection.extract(instagram_item())
    assert collection_error.value.code == "UNSUPPORTED_SOURCE"
    assert [call[1] for call in collection_calls] == [False]

    class ErrorYdl:
        def __init__(self, message):
            self.message = message

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError(self.message)

    for message, expected, attempts in (
        ("Sign in to access this content", "AUTH_REQUIRED", 1),
        ("HTTP Error 429: Too Many Requests", "RATE_LIMITED", 3),
    ):
        seen = []
        mapped = InstagramExtractor(
            FakeTranscriber(),
            tmp_path / f"{expected}-{attempts}",
            ydl_factory=lambda options: seen.append(options) or ErrorYdl(message),
            max_attempts=attempts,
            backoff_seconds=0,
        )
        with pytest.raises(AppError) as error:
            await mapped._info(REEL_A)
        assert error.value.code == expected
        assert len(seen) == attempts
        if expected == "AUTH_REQUIRED":
            assert error.value.permanent is False  # explicit Retry can follow cookie configuration


async def test_optional_cookie_path_is_passed_without_reading_or_logging_contents(tmp_path, caplog):
    """Only the operator's cookie path crosses into yt-dlp options, never file contents."""
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("private-cookie-value", encoding="utf-8")
    extractor, _, options = extractor_for(tmp_path, cookies_file=cookie_file)
    await extractor._info(REEL_A)
    assert options[0]["cookiefile"] == str(cookie_file)
    assert "private-cookie-value" not in caplog.text


async def test_caption_only_is_durable_and_retry_skips_yt_dlp(tmp_path, session_factory):
    """A meaningful caption survives analysis failure without being stored as transcript."""
    caption = "This detailed caption explains the technique and why the demonstration matters."
    extractor, calls, _ = extractor_for(
        tmp_path,
        reel_info(description=caption, formats=[{"acodec": "none"}]),
    )
    provider = FakeLlmProvider(analyze_failures=1)
    worker = make_worker(session_factory, extractor, provider)
    item = await ingest_reel(session_factory)

    assert await worker.process_one()
    retried = await apply_item_action(session_factory, 42, item.id, "retry")
    assert retried is not None and retried.processing_stage == "ANALYZING"
    assert await worker.process_one()

    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        contents = (
            await session.scalars(select(Content).where(Content.source_id == source.id))
        ).all()
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.analysis_completeness == "CAPTION_ONLY"
    assert source.extraction_status == "READY"
    assert source.metadata_json["instagram_caption_only"] is True
    assert {content.kind for content in contents} == {ContentKind.DESCRIPTION}
    assert provider.calls[0][0].text == caption
    assert len(calls) == 1 and calls[0][1] is False
    assert extractor.transcriber.calls == 0
    assert "подписи Reel" in format_ready_item(stored)


async def test_visual_only_reel_reuses_frames_and_profile_language(
    tmp_path, session_factory, monkeypatch
):
    """Silent Reel notes use existing vision, durable VISUAL_NOTES, and profile language."""
    extractor, calls, _ = extractor_for(
        tmp_path,
        reel_info(formats=[{"acodec": "none"}]),
    )
    provider = FakeLlmProvider(vision=True, describe_notes="The video shows a short demonstration.")
    worker = make_worker(session_factory, extractor, provider)
    item = await ingest_reel(session_factory)
    captured_durations = []

    def fake_frames(video, work_dir, **kwargs):
        frame_dir = Path(work_dir)
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame = frame_dir / "frame_0001.jpg"
        frame.write_bytes(b"frame")
        captured_durations.append(kwargs["duration_seconds"])
        return [frame]

    monkeypatch.setattr("app.services.processing.extract_representative_frames", fake_frames)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        user = await session.get(User, stored.user_id)
        user.profile_json = UserProfile(preferred_language="en").model_dump()
        await session.commit()

    assert await worker.process_one()
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        notes = await session.scalar(
            select(Content).where(
                Content.source_id == source.id,
                Content.kind == ContentKind.VISUAL_NOTES,
            )
        )
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.analysis_completeness == "VISUAL_ONLY"
    assert notes is not None and "demonstration" in notes.text
    assert source.metadata_json["instagram_visual_only"] is True
    assert provider.describe_languages == ["en"]
    assert captured_durations == [30]
    assert [call[1] for call in calls] == [False, False, True]
    analysis_content = provider.calls[0][0]
    assert "INSTAGRAM" in analysis_content.metadata["successful_source_types"]


async def test_stt_transcript_and_visual_context_resume_without_redownload(
    tmp_path, session_factory
):
    """Durable transcript and description are reused when aggregate analysis is retried."""
    extractor, calls, _ = extractor_for(tmp_path)
    provider = FakeLlmProvider(analyze_failures=1)
    worker = make_worker(session_factory, extractor, provider)
    item = await ingest_reel(session_factory)

    assert await worker.process_one()
    assert (await apply_item_action(session_factory, 42, item.id, "retry")) is not None
    assert await worker.process_one()

    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        rows = (await session.scalars(select(Content).where(Content.source_id == source.id))).all()
    assert stored.processing_status is ProcessingStatus.READY
    assert {row.kind for row in rows} == {ContentKind.TRANSCRIPT, ContentKind.DESCRIPTION}
    assert source.extraction_status == "READY"
    assert [call[1] for call in calls] == [False, True]
    assert extractor.transcriber.calls == 1
    message = build_user_message(provider.calls[-1][0], UserProfile(), [])
    assert "SOURCE CONTEXT" in message
    assert reel_info()["description"] in message


async def test_partial_retry_reextracts_instagram_only_and_marks_failed_source_prompt(
    session_factory, tmp_path
):
    """Failed Reel retry keeps the READY WEB checkpoint and identifies missing content."""

    class FlakyInstagramExtractor:
        def __init__(self):
            self.temp_dir = tmp_path / "instagram"
            self.attempts = 0
            self.fail = True

        async def extract(self, source, **kwargs):
            self.attempts += 1
            if self.fail:
                raise AppError("AUTH_REQUIRED", "Instagram access is unavailable")
            return NormalizedContent(
                source_type=SourceType.INSTAGRAM,
                text="Transcript from the Reel",
                url=source.source_url,
                metadata={"via_stt": True},
            )

    class CountingWebExtractor:
        def __init__(self):
            self.calls = 0

        async def extract(self, source):
            self.calls += 1
            return NormalizedContent(
                source_type=SourceType.WEB,
                text="Article body from the sibling source",
                url=source.source_url,
            )

    item = await ingest_reel(
        session_factory,
        text=f"Compare sources {REEL_A} https://example.com/article",
    )
    provider = FakeLlmProvider()
    instagram = FlakyInstagramExtractor()
    web = CountingWebExtractor()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            web_extractor=web,
            instagram_extractor=instagram,
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one()
    first_content = provider.calls[0][0]
    first_prompt = build_user_message(first_content, UserProfile(), [])
    assert "FAILED SOURCE: index=0 type=INSTAGRAM reason=AUTH_REQUIRED" in first_prompt
    assert "Do not infer or invent them" in first_prompt
    async with session_factory() as session:
        partial = await session.get(Item, item.id)
        statuses = list(
            await session.scalars(
                select(ItemSource.extraction_status)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        )
    assert partial.analysis_completeness == "PARTIAL"
    assert statuses == ["FAILED", "READY"]

    instagram.fail = False
    assert await apply_item_action(session_factory, 42, item.id, "retry")
    assert await worker.process_one()
    async with session_factory() as session:
        completed = await session.get(Item, item.id)
        statuses = list(
            await session.scalars(
                select(ItemSource.extraction_status)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        )
    assert completed.processing_status is ProcessingStatus.READY
    assert completed.analysis_completeness == "TRANSCRIPT_ONLY"
    assert statuses == ["READY", "READY"]
    assert instagram.attempts == 2
    assert web.calls == 1
    assert "Transcript from the Reel" in provider.calls[-1][0].text
    assert "Article body from the sibling source" in provider.calls[-1][0].text


def test_partial_result_reports_instagram_access_failure_to_user():
    """A successful sibling's result still explains why its Reel source is missing."""
    item = Item(
        id=1,
        user_id=1,
        source_type=SourceType.TEXT,
        title="Смешанный результат",
        analysis_completeness="PARTIAL",
    )
    failed_reel = ItemSource(
        id=1,
        item_id=1,
        source_index=0,
        source_type=SourceType.INSTAGRAM,
        extraction_status="FAILED",
        error_code="AUTH_REQUIRED",
    )

    message = format_ready_item(item, [failed_reel])

    assert "частичный" in message
    assert "Reel: доступ требует авторизации Instagram." in message
    assert format_instagram_failure_reason("RATE_LIMITED") == (
        "обработка временно ограничена Instagram"
    )
    assert format_instagram_failure_reason("UNSUPPORTED_SOURCE") == (
        "ссылка на этот Reel не поддерживается"
    )


async def test_auth_failure_is_controlled_and_keeps_manual_retry(tmp_path, session_factory):
    """Authentication errors show actionable Russian copy and retain the common Retry action."""

    class LoginRequiredYdl:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("Login required to view this Reel")

    extractor = InstagramExtractor(
        FakeTranscriber(),
        tmp_path / "instagram",
        ydl_factory=lambda options: LoginRequiredYdl(),
        max_attempts=1,
        backoff_seconds=0,
    )
    item = await ingest_reel(session_factory)
    worker = make_worker(session_factory, extractor, FakeLlmProvider())
    assert await worker.process_one()

    class CapturingBot:
        messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((text, kwargs))

    bot = CapturingBot()
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
    await send_item_failure(bot, session_factory, stored)

    assert len(bot.messages) == 1
    message, options = bot.messages[0]
    assert "требует авторизации Instagram" in message
    assert "INSTAGRAM_COOKIES_FILE" in message
    buttons = [button for row in options["reply_markup"].inline_keyboard for button in row]
    assert any(button.callback_data == f"item:retry:{item.id}" for button in buttons)


async def test_cancelled_extraction_cleans_its_unique_temp_directory(tmp_path):
    """Cancellation propagates while the extractor's owned temporary directory is removed."""
    started = asyncio.Event()

    class BlockingTranscriber:
        async def transcribe(self, audio_path, **kwargs):
            started.set()
            await asyncio.Event().wait()

    extractor, _, _ = extractor_for(tmp_path, transcriber=BlockingTranscriber())
    task = asyncio.create_task(extractor.extract(instagram_item()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "instagram").iterdir()) == []


async def test_worker_timeout_does_not_wait_for_yt_dlp_thread_and_defers_cleanup(
    tmp_path, session_factory
):
    """A timed-out Item is released while its isolated downloader directory stays leased."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    info = reel_info()

    class BlockingDownloadYdl(FakeInstagramYdl):
        def extract_info(self, url, download=False):
            if download:
                started.set()
                try:
                    release.wait(timeout=2)
                finally:
                    finished.set()
            return super().extract_info(url, download)

    def factory(options):
        return BlockingDownloadYdl(options, info, [], b"fake-media")

    extractor = InstagramExtractor(
        FakeTranscriber(),
        tmp_path / "instagram",
        ydl_factory=factory,
        max_attempts=1,
        backoff_seconds=0,
    )
    item = await ingest_reel(session_factory)
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(FakeLlmProvider()),
            PriorityEngine(),
            instagram_extractor=extractor,
        ),
        poll_seconds=0.01,
        processing_timeout_seconds=0.03,
    )
    processing = asyncio.create_task(worker.process_one())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        done, _ = await asyncio.wait({processing}, timeout=0.2)
        assert processing in done
        assert await processing is True

        async with session_factory() as session:
            stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.FAILED
        assert stored.error_code == "PROCESSING_TIMEOUT"

        temp_dirs = list((tmp_path / "instagram").iterdir())
        assert len(temp_dirs) == 1
        assert temp_dirs[0].name.startswith("ig-ytdlp-")
    finally:
        release.set()
        if not processing.done():
            await processing

    assert await asyncio.to_thread(finished.wait, 1)
    for _ in range(100):
        if not list((tmp_path / "instagram").iterdir()):
            break
        await asyncio.sleep(0.01)
    assert list((tmp_path / "instagram").iterdir()) == []
