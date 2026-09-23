import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendVideo
from aiogram.types import ReplyParameters
from sqlalchemy import select

from app.domain.enums import ItemType, ProcessingStatus, SourceType
from app.extractors import youtube as youtube_module
from app.extractors.subprocess_runner import run_killable_subprocess
from app.extractors.youtube import YoutubeExtractor
from app.services.actions import apply_item_action
from app.services.delivery import (
    ITEM_FAILED,
    ITEM_READY,
    ITEM_VIDEO_PREFIX,
    PROFILE_UPDATED,
    TELEGRAM_MAX_UPLOAD_BYTES,
    DeliveryWorker,
    enqueue_item_delivery,
    enqueue_item_video_delivery,
    enqueue_profile_delivery,
    requeue_sending_deliveries,
)
from app.services.ingestion import ingest_message
from app.storage.models import Delivery, Item, ItemSource, ProfileUpdateJob
from tests.fakes import FakeTranscriber


class FakeBot:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.messages: list[tuple[int, str]] = []
        self.message_kwargs: list[dict] = []
        self.media_sends: list[tuple[int, str, object, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))
        self.message_kwargs.append(kwargs)

    async def send_video(self, chat_id, video, **kwargs):
        self.media_sends.append((chat_id, "video", video, kwargs))
        return SimpleNamespace(video=SimpleNamespace(file_id="cached-telegram-video"))

    async def send_document(self, chat_id, document, **kwargs):
        self.media_sends.append((chat_id, "document", document, kwargs))
        return SimpleNamespace(document=SimpleNamespace(file_id="cached-telegram-document"))


class FakeVideoExtractor:
    """Write deterministic source media into the delivery worker's private temp folder."""

    def __init__(self, temp_dir: Path, extension: str = ".mp4", size: int = 11):
        self.temp_dir = temp_dir
        self.extension = extension
        self.size = size
        self.calls: list[tuple[str, int]] = []

    async def download_video(self, source, work_dir, *, byte_limit, include_audio):
        url = source.source_url if hasattr(source, "source_url") else source
        self.calls.append((url, byte_limit, include_audio))
        path = Path(work_dir) / f"clip{self.extension}"
        with path.open("wb") as media:
            media.truncate(self.size)
        return path


async def make_item(session_factory, *, status=ProcessingStatus.READY) -> Item:
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=7777,
            message_id=1,
            text="item",
        )
    ).items[0]
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        stored.processing_status = status
        stored.processing_stage = status.value
        stored.title = "Разобрать материал"
        stored.category = "Обучение"
        stored.item_type = ItemType.LEARN
        stored.priority_score = 80
        if status is ProcessingStatus.FAILED:
            stored.error_code = "LLM_FAILED"
        await session.commit()
    return item


async def test_delivery_worker_sends_ready_item_and_marks_sent(session_factory):
    item = await make_item(session_factory)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await enqueue_item_delivery(session, stored, ITEM_READY)
        await session.commit()

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is True
    expected = (
        "✓ Сохранено\n\n🎯 Разобрать материал\nКатегория: Обучение\nТип: LEARN\n"
        "Приоритет: 80/100\nИнтерес: 2/3"
    )
    assert bot.messages == [(7777, expected)]

    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "SENT"
        assert delivery.attempts == 1
        assert delivery.sent_at is not None


async def _make_ready_video_source(session_factory, source_type: SourceType):
    """Create one READY ItemSource for an on-demand YouTube/Reel delivery test."""
    item = await make_item(session_factory)
    source_url = (
        "https://www.youtube.com/watch?v=clip"
        if source_type is SourceType.YOUTUBE
        else "https://www.instagram.com/reel/clip/"
    )
    async with session_factory() as session:
        source = ItemSource(
            item_id=item.id,
            source_index=0,
            source_type=source_type,
            source_url=source_url,
            extraction_status="READY",
        )
        session.add(source)
        await session.commit()
        return item.id, source.id, source_url


@pytest.mark.parametrize("source_type", [SourceType.YOUTUBE, SourceType.INSTAGRAM])
async def test_video_delivery_downloads_selected_source_and_reuses_telegram_file_id(
    tmp_path, session_factory, source_type
):
    """The outbox downloads one child, cleans temp media, and caches Telegram's file id."""
    item_id, source_id, source_url = await _make_ready_video_source(session_factory, source_type)
    assert await enqueue_item_video_delivery(session_factory, 42, item_id, source_id) == "QUEUED"
    temp_root = tmp_path / source_type.value.lower()
    extractor = FakeVideoExtractor(temp_root)
    bot = FakeBot()
    worker = DeliveryWorker(
        session_factory,
        bot,
        youtube_extractor=extractor,
        instagram_extractor=extractor,
        retry_backoff_seconds=0,
    )

    assert await worker.process_one() is True
    assert extractor.calls == [(source_url, TELEGRAM_MAX_UPLOAD_BYTES, True)]
    assert len(bot.media_sends) == 1
    chat_id, media_kind, uploaded, kwargs = bot.media_sends[0]
    assert chat_id == 7777
    assert media_kind == "video"
    assert not Path(uploaded.path).exists()
    assert kwargs["supports_streaming"] is True
    assert kwargs["reply_parameters"] == ReplyParameters(
        message_id=1,
        allow_sending_without_reply=True,
    )

    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"{ITEM_VIDEO_PREFIX}{source_id}",
            )
        )
        assert delivery.status == "SENT"
        assert delivery.payload_json["telegram_file_id"] == "cached-telegram-video"

    assert await enqueue_item_video_delivery(session_factory, 42, item_id, source_id) == "QUEUED"
    assert await worker.process_one() is True
    assert extractor.calls == [(source_url, TELEGRAM_MAX_UPLOAD_BYTES, True)]
    assert bot.media_sends[1][2] == "cached-telegram-video"


async def test_youtube_download_timeout_kills_worker_before_cleanup_and_advances_outbox(
    tmp_path, session_factory, monkeypatch
):
    """A stuck child is reaped before cleanup, leaving the single delivery worker usable."""
    item_id, source_id, _ = await _make_ready_video_source(session_factory, SourceType.YOUTUBE)
    await enqueue_item_video_delivery(session_factory, 42, item_id, source_id)
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        await enqueue_item_delivery(session, item, ITEM_READY)
        await session.commit()

    started = tmp_path / "download-worker.pid"
    raced_cleanup = tmp_path / "cleanup-raced-worker"
    program = "\n".join(
        [
            "import os, pathlib, sys, time",
            "directory = pathlib.Path(sys.argv[1])",
            "started = pathlib.Path(sys.argv[2])",
            "raced = pathlib.Path(sys.argv[3])",
            "started.write_text(str(os.getpid()))",
            "while True:",
            "    if not directory.is_dir():",
            "        raced.write_text('cleanup raced worker')",
            "        break",
            "    try:",
            "        (directory / 'active').write_text('writing')",
            "    except FileNotFoundError:",
            "        raced.write_text('cleanup raced worker')",
            "        break",
            "    time.sleep(0.005)",
        ]
    )

    async def run_hanging_download(command, payload, **kwargs):
        # Keep the real shared process owner, replacing only yt-dlp with a deterministic writer.
        return await run_killable_subprocess(
            [
                sys.executable,
                "-c",
                program,
                str(kwargs["cleanup_dir"]),
                str(started),
                str(raced_cleanup),
            ],
            payload,
            **kwargs,
        )

    monkeypatch.setattr(youtube_module, "run_killable_subprocess", run_hanging_download)
    extractor = YoutubeExtractor(
        transcriber=FakeTranscriber(),
        temp_dir=tmp_path / "youtube",
        download_timeout_seconds=0.5,
    )
    bot = FakeBot()
    worker = DeliveryWorker(
        session_factory,
        bot,
        youtube_extractor=extractor,
        max_attempts=1,
        retry_backoff_seconds=0,
    )

    assert await worker.process_one() is True
    child_pid = int(started.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not raced_cleanup.exists()
    assert list((tmp_path / "youtube").iterdir()) == []

    assert await worker.process_one() is True
    assert any("Разобрать материал" in text for _, text in bot.messages)
    async with session_factory() as session:
        deliveries = list((await session.scalars(select(Delivery).order_by(Delivery.id))).all())
    assert [delivery.status for delivery in deliveries] == ["FAILED", "SENT"]


async def test_rejected_cached_file_id_falls_back_to_fresh_video_upload(tmp_path, session_factory):
    """A stale Telegram reference triggers one source re-upload in the same request."""
    item_id, source_id, source_url = await _make_ready_video_source(
        session_factory, SourceType.YOUTUBE
    )
    extractor = FakeVideoExtractor(tmp_path / "youtube")

    class RejectCachedFileIdBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.cached_ids = []

        async def send_video(self, chat_id, video, **kwargs):
            if isinstance(video, str):
                self.cached_ids.append(video)
                raise TelegramBadRequest(
                    method=SendVideo(chat_id=chat_id, video=video),
                    message="Bad Request: wrong file identifier/HTTP URL specified",
                )
            return await super().send_video(chat_id, video, **kwargs)

    bot = RejectCachedFileIdBot()
    worker = DeliveryWorker(
        session_factory,
        bot,
        youtube_extractor=extractor,
        retry_backoff_seconds=0,
    )

    assert await enqueue_item_video_delivery(session_factory, 42, item_id, source_id) == "QUEUED"
    assert await worker.process_one() is True
    assert await enqueue_item_video_delivery(session_factory, 42, item_id, source_id) == "QUEUED"
    assert await worker.process_one() is True

    assert bot.cached_ids == ["cached-telegram-video"]
    assert len(extractor.calls) == 2
    assert len(bot.media_sends) == 2
    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"{ITEM_VIDEO_PREFIX}{source_id}",
            )
        )
        assert delivery.status == "SENT"
        assert delivery.payload_json["telegram_file_id"] == "cached-telegram-video"


async def test_video_delivery_sends_non_mp4_as_document(tmp_path, session_factory):
    """Unsupported preview containers remain downloadable attachments in Telegram."""
    item_id, source_id, _ = await _make_ready_video_source(session_factory, SourceType.YOUTUBE)
    await enqueue_item_video_delivery(session_factory, 42, item_id, source_id)
    extractor = FakeVideoExtractor(tmp_path / "youtube", extension=".webm")
    bot = FakeBot()
    worker = DeliveryWorker(
        session_factory,
        bot,
        youtube_extractor=extractor,
        retry_backoff_seconds=0,
    )

    assert await worker.process_one() is True
    assert bot.media_sends[0][1] == "document"
    assert not Path(bot.media_sends[0][2].path).exists()


async def test_video_delivery_rejects_files_above_telegram_upload_limit(tmp_path, session_factory):
    """The Bot API cap is enforced even if an extractor returns an oversized file."""
    item_id, source_id, _ = await _make_ready_video_source(session_factory, SourceType.YOUTUBE)
    await enqueue_item_video_delivery(session_factory, 42, item_id, source_id)
    extractor = FakeVideoExtractor(
        tmp_path / "youtube",
        size=TELEGRAM_MAX_UPLOAD_BYTES + 1,
    )
    bot = FakeBot()
    worker = DeliveryWorker(
        session_factory,
        bot,
        youtube_extractor=extractor,
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    assert await worker.process_one() is True
    assert bot.media_sends == []
    assert (
        bot.messages[-1][1] == "Видео превышает лимит Telegram в 50 MB. Откройте исходную ссылку."
    )
    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item_id,
                Delivery.type == f"{ITEM_VIDEO_PREFIX}{source_id}",
            )
        )
        assert delivery.status == "FAILED"
        assert delivery.attempts == 1
        assert "50 MB" in delivery.last_error


async def test_terminal_cached_file_failure_allows_fresh_download_on_next_tap(session_factory):
    """A stale Telegram file id is cleared when delivery fails permanently."""
    item_id, source_id, _ = await _make_ready_video_source(session_factory, SourceType.YOUTUBE)
    async with session_factory() as session:
        item = await session.get(Item, item_id)
        delivery = Delivery(
            user_id=item.user_id,
            item_id=item_id,
            type=f"{ITEM_VIDEO_PREFIX}{source_id}",
            status="PENDING",
            payload_json={
                "source_id": source_id,
                "telegram_file_id": "stale-telegram-file-id",
                "telegram_media_kind": "video",
            },
        )
        session.add(delivery)
        await session.commit()

    class RejectCachedVideoBot(FakeBot):
        """Simulate Telegram invalidating a previously cached file id."""

        async def send_video(self, chat_id, video, **kwargs):
            if isinstance(video, str):
                raise RuntimeError("file identifier is invalid")
            return await super().send_video(chat_id, video, **kwargs)

    worker = DeliveryWorker(
        session_factory,
        RejectCachedVideoBot(),
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    assert await worker.process_one() is True

    async with session_factory() as session:
        stored = await session.get(Delivery, delivery.id)
        assert stored.status == "FAILED"
        assert stored.payload_json == {"source_id": source_id}
    assert await enqueue_item_video_delivery(session_factory, 42, item_id, source_id) == "QUEUED"


@pytest.mark.parametrize(
    ("source_type", "source_metadata", "expected_original_url"),
    [
        (SourceType.VIDEO, None, None),
        (
            SourceType.TEXT,
            {
                "forwarded": True,
                "forward_origin_type": "channel",
                "forward_source_username": "androiddev",
                "forward_message_id": 8712,
            },
            "https://t.me/androiddev/8712",
        ),
    ],
)
@pytest.mark.parametrize(
    ("delivery_type", "status"),
    [
        (ITEM_READY, ProcessingStatus.READY),
        (ITEM_FAILED, ProcessingStatus.FAILED),
    ],
)
async def test_video_result_replies_to_input_and_keeps_forward_origin_link(
    session_factory,
    source_type,
    source_metadata,
    expected_original_url,
    delivery_type,
    status,
):
    """Keep a clickable reply anchor to the submitted video in the result chat."""
    item = await make_item(session_factory, status=status)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        stored.source_type = source_type
        stored.source_metadata_json = source_metadata
        session.add(
            ItemSource(
                item_id=item.id,
                source_index=0,
                source_type=SourceType.VIDEO,
                source_file_id="video-file",
            )
        )
        await enqueue_item_delivery(session, stored, delivery_type)
        await session.commit()

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is True

    assert bot.message_kwargs[0]["reply_parameters"] == ReplyParameters(
        message_id=1,
        allow_sending_without_reply=True,
    )
    buttons = [
        button for row in bot.message_kwargs[0]["reply_markup"].inline_keyboard for button in row
    ]
    original_links = [button.url for button in buttons if button.text == "↗ Открыть оригинал"]
    assert original_links == ([expected_original_url] if expected_original_url else [])


async def test_delivery_worker_retries_telegram_failure_without_losing_intent(session_factory):
    item = await make_item(session_factory, status=ProcessingStatus.FAILED)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await enqueue_item_delivery(session, stored, ITEM_FAILED, reopen=True)
        await session.commit()

    bot = FakeBot(failures=1)
    worker = DeliveryWorker(
        session_factory,
        bot,
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    assert await worker.process_one() is True
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "PENDING"
        assert delivery.attempts == 1
        assert "telegram unavailable" in delivery.last_error

    assert await worker.process_one() is True
    assert len(bot.messages) == 1
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "SENT"
        assert delivery.attempts == 2


async def test_retry_cancels_pending_stale_failure_delivery(session_factory):
    item = await make_item(session_factory, status=ProcessingStatus.FAILED)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await enqueue_item_delivery(session, stored, ITEM_FAILED, reopen=True)
        await session.commit()

    retried = await apply_item_action(session_factory, 42, item.id, "retry")
    assert retried is not None
    assert retried.processing_status is ProcessingStatus.QUEUED

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is False
    assert bot.messages == []
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery).where(Delivery.type == ITEM_FAILED))
        assert delivery.status == "CANCELLED"


async def test_requeue_interrupted_sending_delivery_after_restart(session_factory):
    item = await make_item(session_factory)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        delivery = await enqueue_item_delivery(session, stored, ITEM_READY)
        delivery.status = "SENDING"
        delivery.attempts = 1
        await session.commit()

    assert await requeue_sending_deliveries(session_factory) == 1
    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery))
        assert delivery.status == "PENDING"
        assert delivery.attempts == 1


async def test_profile_delivery_uses_telegram_chat_and_stable_payload(session_factory):
    await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=7777,
        message_id=1,
        text="x",
    )
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        job = ProfileUpdateJob(user_id=item.user_id, instruction="i", status="DONE")
        session.add(job)
        await session.flush()
        await enqueue_profile_delivery(
            session,
            user_id=item.user_id,
            profile_update_job_id=job.id,
            changed=["profession"],
        )
        await session.commit()

    bot = FakeBot()
    worker = DeliveryWorker(session_factory, bot, retry_backoff_seconds=0)
    assert await worker.process_one() is True
    assert bot.messages == [(7777, "Профиль обновлён: profession")]

    async with session_factory() as session:
        delivery = await session.scalar(select(Delivery).where(Delivery.type == PROFILE_UPDATED))
        assert delivery.status == "SENT"
