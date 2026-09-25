import asyncio
import hashlib

import pytest
from sqlalchemy import select

from app.domain.enums import ContentKind, ProcessingStatus
from app.domain.models import DEFAULT_PROFILE, NormalizedContent
from app.domain.priority import PriorityEngine
from app.llm.base import LlmError
from app.services.analysis import CHUNK_SUMMARY_GENERATOR_VERSION, Analyzer, split_text
from app.services.delivery import ITEM_FAILED, ITEM_READY
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Delivery, Item
from app.workers.processing import ProcessingWorker, requeue_stale
from tests.fakes import FakeLlmProvider, make_analysis


def make_worker(session_factory, provider):
    pipeline = ProcessingPipeline(Analyzer(provider), PriorityEngine())
    return ProcessingWorker(
        session_factory,
        pipeline,
        poll_seconds=0.01,
    )


async def seed(session_factory, text="Изучить AI agents", message_id=1):
    return (
        await ingest_message(
            session_factory, telegram_user_id=42, chat_id=42, message_id=message_id, text=text
        )
    ).items[0]


async def get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


async def test_text_pipeline_end_to_end(session_factory):
    item = await seed(session_factory)
    provider = FakeLlmProvider()
    worker = make_worker(session_factory, provider)

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    analysis = make_analysis()
    assert stored.title == analysis.title
    assert stored.summary == analysis.summary
    assert stored.category == analysis.category
    assert stored.item_type == analysis.item_type
    assert stored.tags_json == analysis.tags
    assert stored.importance == analysis.importance
    assert stored.goal_fit == analysis.goal_fit
    assert stored.estimated_action_minutes == 25
    assert stored.priority_score == PriorityEngine().score(analysis)
    assert stored.next_action == analysis.next_action
    assert stored.language == "ru"
    assert stored.confidence == pytest.approx(analysis.confidence)
    assert stored.processing_stage == "READY"
    # Исходный текст пользователя сохранён вместе с результатом
    assert stored.user_note == "Изучить AI agents"

    # LLM получил normalized content, default-профиль и список существующих категорий
    ((content, profile, categories),) = provider.calls
    assert content.text == "Изучить AI agents"
    assert profile == DEFAULT_PROFILE
    assert categories == []
    assert provider.summarize_calls == []

    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item.id,
                Delivery.type == ITEM_READY,
            )
        )
    assert delivery is not None
    assert delivery.status == "PENDING"


def test_split_text_preserves_all_content():
    text = "абв" * 11
    chunks = split_text(text, 7)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 7 for chunk in chunks)


def test_split_text_rejects_invalid_overlap():
    with pytest.raises(ValueError):
        split_text("text", 4, 4)


def test_split_text_overlap_stops_at_eof_without_redundant_tail():
    chunks = split_text("abcdefghijklmnopq", 10, 2)
    assert chunks == ["abcdefghij", "ijklmnopq"]


def test_split_text_prefers_paragraph_boundary():
    text = "first paragraph\n\nsecond paragraph"
    chunks = split_text(text, 20)
    assert chunks == ["first paragraph\n\n", "second paragraph"]


def test_split_text_never_exceeds_max_chars_at_boundary():
    chunks = split_text("abcd\n\nrest", 5)
    assert chunks == ["abcd\n", "\nrest"]
    assert all(len(chunk) <= 5 for chunk in chunks)


def test_split_text_short_intro_with_overlap_keeps_all_content():
    text = "intro\n\n" + "x" * 40_000
    overlap = 1_000
    chunks = split_text(text, 30_000, overlap)

    assert all(chunks)
    assert all(len(chunk) <= 30_000 for chunk in chunks)
    reconstructed = chunks[0] + "".join(chunk[overlap:] for chunk in chunks[1:])
    assert reconstructed == text


async def test_long_content_is_summarized_before_final_analysis(session_factory):
    await seed(session_factory, text="x" * 25)
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider, chunk_size_chars=10), PriorityEngine()),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    assert provider.summarize_calls == ["x" * 10, "x" * 10, "x" * 5]
    assert len(provider.calls[0][0].text) <= 10
    async with session_factory() as session:
        summaries = (
            await session.scalars(select(Content).where(Content.kind == ContentKind.CHUNK_SUMMARY))
        ).all()
        assert len(summaries) == 3


async def test_chunk_summaries_resume_without_repeating_completed_work(session_factory):
    await seed(session_factory, text="x" * 25)

    class PartialFailureProvider(FakeLlmProvider):
        fail_after_first = True

        async def summarize_chunk(self, text):
            if self.fail_after_first and len(self.summarize_calls) == 1:
                self.summarize_calls.append(text)
                self.fail_after_first = False
                raise LlmError("LLM_FAILED", "summarize failed")
            return await super().summarize_chunk(text)

    provider = PartialFailureProvider()
    analyzer = Analyzer(provider, chunk_size_chars=10)
    async with session_factory() as session:
        item = await session.get(Item, 1)
        with pytest.raises(LlmError):
            await analyzer.analyze(
                NormalizedContent(source_type=item.source_type, text="x" * 25),
                session,
                item.user_id,
                DEFAULT_PROFILE,
                item.id,
            )
        stored = (
            await session.scalars(
                select(Content).where(
                    Content.item_id == item.id,
                    Content.kind == ContentKind.CHUNK_SUMMARY,
                )
            )
        ).all()
        assert len(stored) == 1

    async with session_factory() as session:
        item = await session.get(Item, 1)
        await analyzer.analyze(
            NormalizedContent(source_type=item.source_type, text="x" * 25),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )
    assert provider.summarize_calls == ["x" * 10, "x" * 10, "x" * 10, "x" * 5]


async def test_legacy_chunk_summary_without_content_hash_is_not_reused(session_factory):
    item = await seed(session_factory, text="x" * 15)
    async with session_factory() as session:
        session.add(
            Content(
                item_id=item.id,
                kind=ContentKind.CHUNK_SUMMARY,
                text="stale summary",
                metadata_json={
                    "stage": "chunk",
                    "chunk_index": 0,
                    "chunk_size_chars": 10,
                    "overlap_chars": 0,
                },
            )
        )
        await session.commit()

    provider = FakeLlmProvider()
    analyzer = Analyzer(provider, chunk_size_chars=10)
    async with session_factory() as session:
        await analyzer.analyze(
            NormalizedContent(source_type=item.source_type, text="x" * 15),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )

    assert provider.summarize_calls == ["x" * 10, "x" * 5]


@pytest.mark.parametrize("generator_version", [None, CHUNK_SUMMARY_GENERATOR_VERSION - 1])
async def test_incompatible_chunk_summary_is_recomputed_in_place(
    session_factory, generator_version
):
    """A prompt change replaces derived evidence instead of reusing or duplicating it."""
    item = await seed(session_factory, text="x" * 15)
    metadata = {
        "stage": "chunk",
        "chunk_index": 0,
        "chunk_size_chars": 10,
        "overlap_chars": 0,
        "chunk_sha256": hashlib.sha256(("x" * 10).encode()).hexdigest(),
    }
    if generator_version is not None:
        metadata["generator_version"] = generator_version

    async with session_factory() as session:
        old_row = Content(
            item_id=item.id,
            kind=ContentKind.CHUNK_SUMMARY,
            text="old topic-only summary",
            metadata_json=metadata,
        )
        session.add(old_row)
        await session.commit()
        old_id = old_row.id

    provider = FakeLlmProvider()
    analyzer = Analyzer(provider, chunk_size_chars=10)
    async with session_factory() as session:
        await analyzer.analyze(
            NormalizedContent(source_type=item.source_type, text="x" * 15),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )

    assert provider.summarize_calls == ["x" * 10, "x" * 5]
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Content)
                .where(Content.item_id == item.id, Content.kind == ContentKind.CHUNK_SUMMARY)
                .order_by(Content.id)
            )
        ).all()
    assert len(rows) == 2
    assert rows[0].id == old_id
    assert [row.metadata_json["generator_version"] for row in rows] == [
        CHUNK_SUMMARY_GENERATOR_VERSION,
        CHUNK_SUMMARY_GENERATOR_VERSION,
    ]


async def test_compatible_versioned_chunk_summaries_are_reused(session_factory):
    """Compatible durable summaries avoid another provider call after restart."""
    item = await seed(session_factory, text="x" * 15)
    async with session_factory() as session:
        session.add(
            Content(
                item_id=item.id,
                kind=ContentKind.CHUNK_SUMMARY,
                text="v2 first chunk",
                metadata_json={
                    "stage": "chunk",
                    "chunk_index": 0,
                    "chunk_size_chars": 10,
                    "overlap_chars": 0,
                    "chunk_sha256": hashlib.sha256(("x" * 10).encode()).hexdigest(),
                    "generator_version": CHUNK_SUMMARY_GENERATOR_VERSION,
                },
            )
        )
        await session.commit()

    provider = FakeLlmProvider()
    analyzer = Analyzer(provider, chunk_size_chars=10)
    async with session_factory() as session:
        await analyzer.analyze(
            NormalizedContent(source_type=item.source_type, text="x" * 15),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )

    assert provider.summarize_calls == ["x" * 5]


async def test_long_content_aggregate_preserves_order_and_late_conclusion(session_factory):
    """Ordered framing gives final analysis the conclusion without persisting markers."""
    item = await seed(session_factory, text="x" * 1_000)

    class OutcomeProvider(FakeLlmProvider):
        chunk_outputs = [
            "Background: tools were preloaded for convenience.",
            "Users often enabled a large collection globally.",
            "The controlled test found extra context reduced task accuracy.",
            "Conclusion: add skills only after failure.",
        ]

        async def summarize_chunk(self, text):
            self.summarize_calls.append(text)
            return self.chunk_outputs[len(self.summarize_calls) - 1]

    provider = OutcomeProvider()
    analyzer = Analyzer(provider, chunk_size_chars=300)
    async with session_factory() as session:
        await analyzer.analyze(
            NormalizedContent(source_type=item.source_type, text="x" * 1_000),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )

    aggregate = provider.calls[0][0].text
    assert len(aggregate) <= 300
    assert "CHUNK 1/4:" in aggregate
    assert "CHUNK 4/4:" in aggregate
    assert "Conclusion: add skills only after failure." in aggregate
    assert aggregate.index("CHUNK 1/4:") < aggregate.index("CHUNK 4/4:")
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Content)
                .where(Content.item_id == item.id, Content.kind == ContentKind.CHUNK_SUMMARY)
                .order_by(Content.id)
            )
        ).all()
    assert [row.text for row in rows] == provider.chunk_outputs
    assert provider.calls[0][0].metadata["_analysis_chunk_summary_count"] == 4


async def test_chunk_summary_provider_call_has_no_open_sqlite_transaction(session_factory):
    """Each provider request runs between short checkpoint transactions."""
    item = await seed(session_factory, text="x" * 25)

    class TransactionCheckingProvider(FakeLlmProvider):
        session = None

        async def summarize_chunk(self, text):
            assert not self.session.in_transaction()
            return await super().summarize_chunk(text)

    provider = TransactionCheckingProvider()
    analyzer = Analyzer(provider, chunk_size_chars=10)
    async with session_factory() as session:
        provider.session = session
        await analyzer.analyze(
            NormalizedContent(source_type=item.source_type, text="x" * 25),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )
    assert len(provider.summarize_calls) == 3


async def test_existing_categories_passed_to_provider(session_factory):
    await seed(session_factory, message_id=1)
    # Первый Item уже обработан и имеет категорию — второй запрос получает её список
    first_provider = FakeLlmProvider()
    await make_worker(session_factory, first_provider).process_one()

    await seed(session_factory, message_id=2)
    second_provider = FakeLlmProvider()
    await make_worker(session_factory, second_provider).process_one()

    ((_, _, categories),) = second_provider.calls
    assert categories == [make_analysis().category]


async def test_transcript_checkpoint_lookup_releases_read_transaction(session_factory):
    """A media download starts only after its persisted STT lookup releases SQLite."""
    item = await seed(session_factory)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        await ProcessingPipeline._transcript_checkpoints(session, stored)
        assert not session.in_transaction()


async def test_analyzer_releases_category_read_before_provider_call(session_factory):
    """LLM latency must not keep the analysis session's SQLite read transaction open."""
    item = await seed(session_factory)

    class TransactionCheckingProvider(FakeLlmProvider):
        session = None

        async def analyze(self, content, profile, categories):
            assert not self.session.in_transaction()
            return await super().analyze(content, profile, categories)

    provider = TransactionCheckingProvider()
    async with session_factory() as session:
        provider.session = session
        await Analyzer(provider).analyze(
            NormalizedContent(source_type=item.source_type, text="short"),
            session,
            item.user_id,
            DEFAULT_PROFILE,
            item.id,
        )


async def test_invalid_llm_output_fails_item_without_losing_text(session_factory):
    item = await seed(session_factory)
    provider = FakeLlmProvider(error=LlmError("INVALID_LLM_OUTPUT", "bad json"))
    worker = make_worker(session_factory, provider)

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    assert stored.error_code == "INVALID_LLM_OUTPUT"
    assert stored.user_note == "Изучить AI agents"
    assert stored.title is None


async def test_provider_failure_fails_item_with_llm_code(session_factory):
    item = await seed(session_factory)
    provider = FakeLlmProvider(error=LlmError("LLM_FAILED", "provider unreachable"))
    worker = make_worker(session_factory, provider)

    assert await worker.process_one() is True

    stored = await get_item(session_factory, item.id)
    assert stored.error_code == "LLM_FAILED"
    assert stored.user_note == "Изучить AI agents"


async def test_provider_failure_persists_retry_delivery(session_factory):
    item = await seed(session_factory)
    provider = FakeLlmProvider(error=LlmError("LLM_FAILED", "provider unreachable"))
    worker = make_worker(session_factory, provider)
    assert await worker.process_one() is True
    async with session_factory() as session:
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.item_id == item.id,
                Delivery.type == ITEM_FAILED,
            )
        )
    assert delivery is not None
    assert delivery.status == "PENDING"


class BlockingProvider:
    """Провайдер, блокирующийся до release: имитация долгого LLM-вызова."""

    def __init__(self):
        self.release = asyncio.Event()
        self.calls = 0

    async def analyze(self, content, profile, categories):
        self.calls += 1
        await self.release.wait()
        return make_analysis()


async def _wait_for_stage(session_factory, item_id, stage, attempts=100):
    for _ in range(attempts):
        stored = await get_item(session_factory, item_id)
        if stored.processing_stage == stage:
            return stored
        await asyncio.sleep(0.02)
    raise AssertionError(f"stage {stage} not reached, last={stored.processing_stage}")


async def test_processing_stage_is_durable_during_analysis(session_factory):
    # Регрессия: другая сессия обязана видеть ANALYZING, пока провайдер блокирует —
    # иначе processing_stage не является durable checkpoint (D-001).
    item = await seed(session_factory)
    provider = BlockingProvider()
    worker = make_worker(session_factory, provider)
    task = asyncio.create_task(worker.process_one())

    stored = await _wait_for_stage(session_factory, item.id, "ANALYZING")
    assert stored.processing_status is ProcessingStatus.PROCESSING

    provider.release.set()
    await asyncio.wait_for(task, timeout=5)
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY


async def test_requeue_preserves_meaningful_stage(session_factory):
    # Restart после падения в ANALYZING: стадия сохраняется, REQUEUED ставится
    # только вместо безынформативного маркера PROCESSING.
    item = await seed(session_factory)
    async with session_factory() as session:
        stored = await session.get(Item, item.id)
        stored.processing_status = ProcessingStatus.PROCESSING
        stored.processing_stage = "ANALYZING"
        await session.commit()

    assert await requeue_stale(session_factory) == 1
    updated = await get_item(session_factory, item.id)
    assert updated.processing_status is ProcessingStatus.QUEUED
    assert updated.processing_stage == "ANALYZING"


class CountingProvider:
    def __init__(self):
        self.calls = 0

    async def analyze(self, content, profile, categories):
        self.calls += 1
        return make_analysis()


async def test_checkpoint_resumable_llm_not_called_twice(session_factory):
    # Полный сценарий ревью: ANALYZING -> death -> requeue -> claim -> checkpoint
    # survives -> resume -> LLM ok -> analysis persisted at PRIORITIZING -> death ->
    # restart -> priority из persisted analysis -> LLM второй раз не вызывается.
    item = await seed(session_factory)
    blocker = BlockingProvider()
    task = asyncio.create_task(make_worker(session_factory, blocker).process_one())
    await _wait_for_stage(session_factory, item.id, "ANALYZING")
    task.cancel()  # процесс умирает во время дорогого LLM-вызова
    with pytest.raises(asyncio.CancelledError):
        await task

    await requeue_stale(session_factory)
    counter = CountingProvider()
    assert await make_worker(session_factory, counter).process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.priority_score is not None
    assert counter.calls == 1  # resume: ровно один успешный вызов

    # Крэш сразу после PRIORITIZING checkpoint: analysis персистен, READY не успел
    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.PROCESSING
        row.processing_stage = "PRIORITIZING"
        row.priority_score = None
        await session.commit()

    # restart: сначала startup recovery возвращает PROCESSING -> QUEUED
    assert await requeue_stale(session_factory) == 1
    assert await make_worker(session_factory, counter).process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.processing_status is ProcessingStatus.READY
    assert stored.priority_score == PriorityEngine().score(make_analysis())
    assert counter.calls == 1  # LLM второй раз не вызывался
