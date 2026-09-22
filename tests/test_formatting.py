import json

import pytest

from app.bot.formatting import format_categories, format_item_list, format_ready_item
from app.domain.enums import ItemType, SourceType
from app.domain.models import AnalysisResult
from app.llm.base import LlmError
from app.llm.openai import OpenAiProvider
from app.storage.models import Item
from tests.fakes import invalid_analysis_json, make_analysis


def make_ready_item() -> Item:
    analysis = make_analysis()
    return Item(
        id=1,
        user_id=1,
        source_type=SourceType.TEXT,
        title=analysis.title,
        summary=analysis.summary,
        category=analysis.category,
        item_type=analysis.item_type,
        tags_json=analysis.tags,
        priority_score=82,
        interest_level=2,
        next_action=analysis.next_action,
        priority_reason=analysis.priority_reason,
    )


def test_format_contains_key_fields():
    text = format_ready_item(make_ready_item())
    assert "🎯 Архитектура AI-агентов" in text
    assert "Категория: AI" in text
    assert f"Тип: {ItemType.LEARN.value}" in text
    assert "Приоритет: 82/100" in text
    assert "Интерес: 2/3" in text
    assert "Разбор подходов к оркестрации агентов." in text
    assert "Следующее действие: Посмотреть блок про tool orchestration" in text
    assert "Почему: Сильно связано с профессиональными целями" in text


def test_youtube_transcript_only_is_explicit_in_user_output():
    item = make_ready_item()
    item.source_type = SourceType.YOUTUBE
    item.analysis_completeness = "TRANSCRIPT_ONLY"

    text = format_ready_item(item)

    assert "Анализ: по транскрипту, без визуальной части" in text


def test_item_list_format_stays_within_telegram_limit():
    items = [make_ready_item() for _ in range(20)]
    for item in items:
        item.title = "x" * 300
    text = format_item_list(items, "Входящие:")
    assert len(text) <= 4096


def test_category_format_stays_within_telegram_limit():
    text = format_categories([(f"category-{index}-{'x' * 300}", index) for index in range(30)])
    assert len(text) <= 4096


def test_openai_parse_valid_json():
    analysis = make_analysis()
    parsed = OpenAiProvider.parse_analysis(analysis.model_dump_json())
    assert parsed.title == analysis.title
    assert parsed.item_type == analysis.item_type


def test_openai_parse_invalid_json_raises_invalid_output():
    with pytest.raises(LlmError) as exc_info:
        OpenAiProvider.parse_analysis(invalid_analysis_json())
    assert exc_info.value.code == "INVALID_LLM_OUTPUT"


def test_openai_parse_garbage_raises_invalid_output():
    with pytest.raises(LlmError) as exc_info:
        OpenAiProvider.parse_analysis("not json at all")
    assert exc_info.value.code == "INVALID_LLM_OUTPUT"


def test_openai_parse_rejects_extra_fields():
    # Регрессия: чужой priority_score от LLM — INVALID_LLM_OUTPUT, а не молчаливое
    # отбрасывание (приоритет считает только код, PRODUCT_SPEC §37).
    raw = json.loads(make_analysis().model_dump_json())
    raw["priority_score"] = 99
    with pytest.raises(LlmError) as exc_info:
        OpenAiProvider.parse_analysis(json.dumps(raw))
    assert exc_info.value.code == "INVALID_LLM_OUTPUT"


def test_adapter_uses_strict_structured_outputs():
    # Регрессия: schema-constrained Structured Outputs (json_schema), не json_object.
    provider = OpenAiProvider(api_key="k", model="gpt-test")
    fmt = provider.response_format
    assert fmt["type"] == "json_schema"
    schema = fmt["json_schema"]["schema"]
    assert fmt["json_schema"]["strict"] is True
    assert schema["additionalProperties"] is False

    # Регрессия сломанного трансформера: properties/$defs — карты имён, их ключи
    # не могут вырезаться keyword-whitelist'ом.
    reference = AnalysisResult.model_json_schema()["properties"]
    assert set(schema["properties"]) == set(reference)
    assert set(schema["required"]) == set(reference)
    item_type_def = schema["$defs"]["ItemType"]
    assert set(item_type_def["enum"]) == {e.value for e in ItemType}
    item_type_prop = schema["properties"]["item_type"]
    item_type_ref = item_type_prop.get("$ref") or item_type_prop["anyOf"][0]["$ref"]
    assert item_type_ref == "#/$defs/ItemType"
