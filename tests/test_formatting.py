import json
from types import SimpleNamespace

import pytest

from app.bot.formatting import (
    format_categories,
    format_item_details,
    format_item_list,
    format_ready_item,
)
from app.domain.enums import ItemType, SourceType
from app.domain.models import DEFAULT_PROFILE, AnalysisResult, NormalizedContent
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


def test_default_ready_card_prioritizes_title_and_summary():
    text = format_ready_item(make_ready_item())
    assert "✓ Сохранено" in text
    assert "🎯 Архитектура AI-агентов" in text
    assert "Разбор подходов к оркестрации агентов." in text
    for hidden_field in (
        "Категория:",
        "Тип:",
        "Приоритет:",
        "Интерес:",
        "Следующее действие:",
        "Почему:",
    ):
        assert hidden_field not in text


def test_youtube_transcript_only_is_explicit_in_user_output():
    item = make_ready_item()
    item.source_type = SourceType.YOUTUBE
    item.analysis_completeness = "TRANSCRIPT_ONLY"

    text = format_ready_item(item)

    assert "⚠️ Анализ по транскрипту — без визуальной части." in text


@pytest.mark.parametrize(
    ("completeness", "source_type", "warning"),
    [
        (
            "VISUAL_ONLY",
            SourceType.VIDEO,
            "⚠️ Анализ только по кадрам — транскрипт недоступен.",
        ),
        (
            "CAPTION_ONLY",
            SourceType.INSTAGRAM,
            "⚠️ Анализ только по подписи — речь не распознана.",
        ),
        (
            "TRANSCRIPT_ONLY",
            SourceType.YOUTUBE,
            "⚠️ Анализ по транскрипту — без визуальной части.",
        ),
        (
            "PARTIAL",
            SourceType.WEB,
            "⚠️ Анализ частичный — часть источников не обработана.",
        ),
    ],
)
def test_completeness_warnings_remain_visible(completeness, source_type, warning):
    item = make_ready_item()
    item.source_type = source_type
    item.analysis_completeness = completeness

    assert warning in format_ready_item(item)


def test_complete_ready_card_has_no_unnecessary_warning():
    item = make_ready_item()
    item.analysis_completeness = "TRANSCRIPT_AND_VISUAL"

    assert "⚠️" not in format_ready_item(item)


def test_details_localize_item_type_and_show_available_system_fields():
    text = format_item_details(make_ready_item())

    assert "ℹ️ Детали" in text
    assert "Категория: AI" in text
    assert "Тип: Изучить" in text
    assert "Приоритет: 82/100" in text
    assert "Интерес: 2/3" in text
    assert "Следующее действие:\nПосмотреть блок про tool orchestration" in text
    assert "Почему приоритет:\nСильно связано с профессиональными целями" in text
    assert "LEARN" not in text


def test_details_omit_missing_optional_fields():
    item = Item(
        id=2,
        user_id=1,
        source_type=SourceType.TEXT,
        title="Sparse",
        category=None,
        item_type=None,
        priority_score=None,
        interest_level=None,
        analysis_completeness=None,
        next_action=None,
        priority_reason=None,
    )

    text = format_item_details(item)
    assert text == "ℹ️ Детали"
    assert "None" not in text
    assert "—" not in text


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


async def test_openai_analysis_retries_truncated_structured_output_twice():
    """Two bounded retries recover a transiently truncated analysis response."""

    class FakeCompletions:
        def __init__(self):
            self.requests = []
            self.responses = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"title":"cut off'),
                            finish_reason="length",
                        )
                    ]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"title":"still cut off'),
                            finish_reason="error",
                        )
                    ]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=make_analysis().model_dump_json()),
                            finish_reason="stop",
                        )
                    ]
                ),
            ]

        async def create(self, **kwargs):
            self.requests.append(kwargs)
            return self.responses.pop(0)

    provider = OpenAiProvider(api_key="k", model="gpt-test")
    completions = FakeCompletions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await provider.analyze(
        NormalizedContent(source_type=SourceType.INSTAGRAM, text="transcript"),
        DEFAULT_PROFILE,
        [],
    )

    assert result.title == make_analysis().title
    assert [request["max_tokens"] for request in completions.requests] == [2048, 4096, 4096]


async def test_openai_analysis_stops_after_three_invalid_responses_without_logging_content():
    """Persistent malformed provider output stays a controlled, privacy-safe Item error."""

    class FakeCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"title":"private transcript fragment'),
                        finish_reason="length",
                    )
                ]
            )

    provider = OpenAiProvider(api_key="k", model="gpt-test")
    completions = FakeCompletions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(LlmError, match="required JSON schema") as exc_info:
        await provider.analyze(
            NormalizedContent(source_type=SourceType.INSTAGRAM, text="transcript"),
            DEFAULT_PROFILE,
            [],
        )

    assert completions.calls == 3
    assert "private transcript fragment" not in str(exc_info.value)


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
