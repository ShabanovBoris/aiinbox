from sqlalchemy import select

from app.domain.enums import ProcessingStatus, SourceType
from app.domain.models import NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.audio import AudioExtractor
from app.services.actions import apply_item_action
from app.services.analysis import Analyzer
from app.services.delivery import ITEM_READY
from app.services.ingestion import ingest_message, ingest_voice
from app.services.processing import ProcessingPipeline
from app.storage.models import Delivery, Item, ItemSource
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeDownloader, FakeLlmProvider, FakeTranscriber


class CompositeWebExtractor:
    """Deterministic source adapter for testing Item-level aggregation and degradation."""

    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.calls: list[str] = []

    async def extract(self, source) -> NormalizedContent:
        self.calls.append(source.source_url)
        if source.source_url in self.failures:
            raise AppError("EXTRACTION_FAILED", f"cannot extract {source.source_url}")
        return NormalizedContent(
            source_type=SourceType.WEB,
            title=f"Page {source.source_url.rsplit('/', 1)[-1]}",
            text=f"CONTENT FROM {source.source_url}",
            url=source.source_url,
        )


def _worker(session_factory, provider, web_extractor, audio_extractor=None):
    """Wire the real pipeline while replacing only external extraction/provider edges."""
    return ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(provider),
            PriorityEngine(),
            web_extractor=web_extractor,
            audio_extractor=audio_extractor,
        ),
        poll_seconds=0.01,
    )


async def test_multi_url_message_runs_one_combined_analysis(session_factory):
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            text=("Сравни эти подходы https://example.com/first и https://example.com/second"),
        )
    ).items[0]
    provider = FakeLlmProvider()
    extractor = CompositeWebExtractor()
    worker = _worker(session_factory, provider, extractor)

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert "CONTENT FROM https://example.com/first" in content.text
    assert "CONTENT FROM https://example.com/second" in content.text
    assert content.user_note == "Сравни эти подходы и"
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.READY
        assert stored.analysis_completeness == "FULL_TEXT"
        sources = (
            await session.scalars(
                select(ItemSource)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        ).all()
        assert [source.extraction_status for source in sources] == ["READY", "READY"]


async def test_failed_embedded_url_degrades_to_text_and_successful_source(session_factory):
    failed_url = "https://example.com/broken"
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            text=f"Главная мысль поста https://example.com/good и {failed_url}",
        )
    ).items[0]
    provider = FakeLlmProvider()
    worker = _worker(session_factory, provider, CompositeWebExtractor({failed_url}))

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert "CONTENT FROM https://example.com/good" in content.text
    assert content.user_note == "Главная мысль поста и"
    assert len(content.metadata["source_failures"]) == 1
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.READY
        assert stored.analysis_completeness == "PARTIAL"
        statuses = list(
            await session.scalars(
                select(ItemSource.extraction_status)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        )
        assert statuses == ["READY", "FAILED"]


async def test_retry_partial_item_reuses_ready_source_and_regenerates_analysis(session_factory):
    failed_url = "https://example.com/retry-me"
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=2,
            text=f"Сравни https://example.com/stable и {failed_url}",
        )
    ).items[0]
    provider = FakeLlmProvider()
    extractor = CompositeWebExtractor({failed_url})
    worker = _worker(session_factory, provider, extractor)

    assert await worker.process_one() is True
    assert len(provider.calls) == 1

    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        delivery = await session.scalar(
            select(Delivery).where(Delivery.item_id == item.id, Delivery.type == ITEM_READY)
        )
        assert stored.analysis_completeness == "PARTIAL"
        delivery.status = "SENT"
        await session.commit()

    extractor.failures.clear()
    retried = await apply_item_action(session_factory, 42, item.id, "retry")
    assert retried is not None
    assert retried.processing_status is ProcessingStatus.QUEUED
    assert retried.processing_stage == "EXTRACTING"

    async with session_factory() as session:
        statuses = list(
            await session.scalars(
                select(ItemSource.extraction_status)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        )
        assert statuses == ["READY", "PENDING"]

    assert await worker.process_one() is True
    assert extractor.calls.count("https://example.com/stable") == 1
    assert extractor.calls.count(failed_url) == 2
    assert len(provider.calls) == 2
    assert failed_url in provider.calls[-1][0].text

    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        delivery = await session.scalar(
            select(Delivery).where(Delivery.item_id == item.id, Delivery.type == ITEM_READY)
        )
        assert stored.processing_status is ProcessingStatus.READY
        assert stored.analysis_completeness == "FULL_TEXT"
        assert delivery.status == "PENDING"


async def test_failed_only_url_with_message_text_still_analyzes_text(session_factory):
    url = "https://example.com/broken"
    message = f"Даже если ссылка недоступна, сохрани эту мысль {url}"
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            text=message,
        )
    ).items[0]
    provider = FakeLlmProvider()
    worker = _worker(session_factory, provider, CompositeWebExtractor({url}))

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert content.text == message
    assert content.metadata["source_failures"][0]["error_code"] == "EXTRACTION_FAILED"
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.READY
        assert stored.analysis_completeness == "PARTIAL"


async def test_failed_bare_url_without_other_content_fails_item(session_factory):
    url = "https://example.com/broken"
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            text=url,
        )
    ).items[0]
    provider = FakeLlmProvider()
    worker = _worker(session_factory, provider, CompositeWebExtractor({url}))

    assert await worker.process_one() is True

    assert provider.calls == []
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.FAILED
        assert stored.error_code == "EXTRACTION_FAILED"


async def test_audio_caption_and_url_are_combined_into_one_analysis(tmp_path, session_factory):
    item = (
        await ingest_voice(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=1,
            file_id="voice-1",
            duration_seconds=15,
            source_type=SourceType.VOICE,
            source_text="Комментарий к голосовому https://example.com/context",
        )
    ).items[0]
    provider = FakeLlmProvider()
    worker = _worker(
        session_factory,
        provider,
        CompositeWebExtractor(),
        AudioExtractor(FakeTranscriber(), FakeDownloader(), tmp_path / "audio"),
    )

    assert await worker.process_one() is True

    assert len(provider.calls) == 1
    content = provider.calls[0][0]
    assert "Голосовая заметка: изучить агентов" in content.text
    assert "CONTENT FROM https://example.com/context" in content.text
    assert content.user_note == "Комментарий к голосовому"
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        assert stored.processing_status is ProcessingStatus.READY
        assert await session.scalar(
            select(ItemSource).where(
                ItemSource.item_id == item.id,
                ItemSource.source_type == SourceType.VOICE,
            )
        )
        assert await session.scalar(
            select(ItemSource).where(
                ItemSource.item_id == item.id,
                ItemSource.source_type == SourceType.WEB,
            )
        )
