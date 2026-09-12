import pytest

from app.domain.models import DEFAULT_PROFILE
from app.domain.priority import PriorityEngine
from app.llm.base import LlmError
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_text
from app.services.processing import ProcessingPipeline
from app.storage.models import Item
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeLlmProvider, make_analysis


def make_worker(session_factory, provider, on_result=None):
    pipeline = ProcessingPipeline(Analyzer(provider), PriorityEngine())
    return ProcessingWorker(session_factory, pipeline, poll_seconds=0.01, on_result=on_result)


async def seed(session_factory, text="Изучить AI agents", message_id=1):
    return await ingest_text(
        session_factory, telegram_user_id=42, chat_id=42, message_id=message_id, text=text
    )


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


async def test_existing_categories_passed_to_provider(session_factory):
    await seed(session_factory, message_id=1)
    # Первый Item уже обработан и имеет категорию — второй запрос получает её список
    first_provider = FakeLlmProvider()
    await make_worker(session_factory, first_provider).process_one()

    second = await seed(session_factory, message_id=2)
    second_provider = FakeLlmProvider()
    await make_worker(session_factory, second_provider).process_one()

    ((_, _, categories),) = second_provider.calls
    assert categories == [make_analysis().category]
    assert second is not None


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
