import asyncio

import pytest
from sqlalchemy import select

from app.domain.enums import ProcessingStatus
from app.domain.models import DEFAULT_PROFILE, NormalizedContent
from app.domain.priority import PriorityEngine
from app.llm.base import LlmError
from app.services.analysis import Analyzer, split_text
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.storage.models import Content, Item
from app.workers.processing import ProcessingWorker, requeue_stale
from tests.fakes import FakeLlmProvider, make_analysis


def make_worker(session_factory, provider, on_result=None, on_failure=None):
    pipeline = ProcessingPipeline(Analyzer(provider), PriorityEngine())
    return ProcessingWorker(
        session_factory,
        pipeline,
        poll_seconds=0.01,
        on_result=on_result,
        on_failure=on_failure,
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
        summaries = (await session.scalars(select(Content))).all()
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
        stored = (await session.scalars(select(Content).where(Content.item_id == item.id))).all()
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


async def test_provider_failure_notifies_retry_surface(session_factory):
    item = await seed(session_factory)
    notified = []

    async def on_failure(failed_item):
        notified.append(failed_item)

    provider = FakeLlmProvider(error=LlmError("LLM_FAILED", "provider unreachable"))
    worker = make_worker(session_factory, provider, on_failure=on_failure)
    assert await worker.process_one() is True
    assert [failed.id for failed in notified] == [item.id]
    assert notified[0].processing_status is ProcessingStatus.FAILED


async def test_result_delivery_failure_keeps_item_ready(session_factory):
    # Auxiliary-операция (доставка) не должна ломать готовый результат
    item = await seed(session_factory)
    provider = FakeLlmProvider()

    async def broken_delivery(delivered):
        raise RuntimeError("telegram down")

    worker = make_worker(session_factory, provider, on_result=broken_delivery)
    assert await worker.process_one() is True
    stored = await get_item(session_factory, item.id)
    assert stored.title == make_analysis().title


async def test_result_delivered_to_callback(session_factory):
    await seed(session_factory)
    provider = FakeLlmProvider()
    delivered: list[Item] = []
    worker = make_worker(session_factory, provider, on_result=delivered.append)
    await worker.process_one()
    assert len(delivered) == 1
    assert delivered[0].title == make_analysis().title


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
