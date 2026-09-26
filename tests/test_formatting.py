import json
from types import SimpleNamespace

import pytest

from app.bot.formatting import (
    format_attention_item,
    format_categories,
    format_item_details,
    format_item_failure,
    format_item_list,
    format_ready_item,
    format_snooze_reminder,
    format_today,
)
from app.bot.keyboards import item_keyboard
from app.bot.presentation import item_display_title
from app.domain.enums import ItemType, ProcessingStatus, SourceType
from app.domain.models import DEFAULT_PROFILE, AnalysisResult, NormalizedContent
from app.llm.base import LlmError
from app.llm.openai import OpenAiProvider
from app.storage.models import Item, ItemSource
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


def test_ready_card_uses_safe_source_title_when_analysis_title_is_missing():
    item = make_ready_item()
    item.title = None
    source = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://www.avito.ru/item?private=1",
    )

    assert "🎯 avito.ru" in format_ready_item(item, [source])
    assert "private" not in format_ready_item(item, [source])


def test_youtube_transcript_only_is_explicit_in_user_output():
    item = make_ready_item()
    item.source_type = SourceType.YOUTUBE
    item.analysis_completeness = "TRANSCRIPT_ONLY"

    text = format_ready_item(item)

    assert "⚠️ Анализ по транскрипту — без визуальной части." in text


def test_display_title_prefers_canonical_title_without_mutating_item():
    item = make_ready_item()
    original_title = item.title
    source = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://www.avito.ru/item?private=1",
    )

    assert item_display_title(item, [source]) == original_title
    assert item.title == original_title


def test_display_title_uses_safe_web_host_and_hides_rejected_or_local_url():
    item = make_ready_item()
    item.title = None
    public = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://www.avito.ru/item?private=1#secret",
    )
    rejected = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://internal.example/item",
        error_code="SECURITY_REJECTED",
    )
    local = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="http://127.0.0.1/admin",
    )

    assert item_display_title(item, [public]) == "avito.ru"
    assert "private" not in item_display_title(item, [public])
    assert item_display_title(item, [rejected]) == "Ссылка"
    assert item_display_title(item, [local]) == "Ссылка"


def test_display_title_uses_persisted_safe_document_filename():
    item = make_ready_item()
    item.title = None
    source = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.DOCUMENT,
        metadata_json={"file_name": "../roadmap.pdf"},
    )

    assert item_display_title(item, [source]) == "Документ — roadmap.pdf"


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        (SourceType.VOICE, "Голосовое"),
        (SourceType.AUDIO, "Аудио"),
        (SourceType.VIDEO, "Видео"),
        (SourceType.YOUTUBE, "YouTube-видео"),
        (SourceType.INSTAGRAM, "Instagram Reel"),
        (SourceType.DOCUMENT, "Документ"),
        (SourceType.TEXT, "Текстовая заметка"),
    ],
)
def test_display_title_has_deterministic_source_type_fallbacks(source_type, expected):
    item = make_ready_item()
    item.title = None
    item.source_type = source_type

    assert item_display_title(item) == expected


def test_display_title_never_exposes_private_note_content_and_composes_sources():
    item = make_ready_item()
    item.title = None
    item.source_type = SourceType.TEXT
    item.user_note = "PRIVATE NOTE CONTENT\nsecond line"
    assert item_display_title(item) == "Текстовая заметка"
    text_source = ItemSource(item_id=item.id, source_index=0, source_type=SourceType.TEXT)
    assert item_display_title(item, [text_source]) == "Текстовая заметка"
    assert "PRIVATE NOTE CONTENT" not in item_display_title(item, [text_source])
    web = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://avito.ru/item",
    )
    youtube = ItemSource(
        item_id=item.id,
        source_index=1,
        source_type=SourceType.YOUTUBE,
        source_url="https://youtube.com/watch?v=abc",
    )
    assert item_display_title(item, [web, youtube]) == "avito.ru + YouTube-видео"


def test_failed_copy_uses_recognizable_title_and_hides_download_code():
    item = make_ready_item()
    item.title = None
    item.processing_status = ProcessingStatus.FAILED
    item.processing_stage = "EXTRACTING"
    item.source_type = SourceType.WEB
    item.telegram_message_id = 123
    item.error_code = "DOWNLOAD_FAILED"
    source = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://avito.ru/item",
        extraction_status="FAILED",
        error_code="DOWNLOAD_FAILED",
        metadata_json={"failure_permanent": False},
    )

    text = format_item_failure(item, [source])

    assert text.startswith("⚠️ avito.ru\n\n")
    assert "Не удалось загрузить содержимое ссылки." in text
    assert "Можно повторить попытку." in text
    assert "DOWNLOAD_FAILED" not in text
    callbacks = {
        button.callback_data
        for row in item_keyboard(item, [source]).inline_keyboard
        for button in row
    }
    assert "item:original:1" in callbacks
    assert "item:retry:1" in callbacks
    assert any(
        button.text == "↗ Статья — avito.ru"
        for row in item_keyboard(item, [source]).inline_keyboard
        for button in row
    )


def test_failed_copy_localizes_transcription_and_keeps_retry_policy():
    item = make_ready_item()
    item.title = None
    item.processing_status = ProcessingStatus.FAILED
    item.processing_stage = "EXTRACTING"
    item.source_type = SourceType.VOICE
    item.error_code = "TRANSCRIPTION_FAILED"
    source = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.VOICE,
        extraction_status="FAILED",
        error_code="TRANSCRIPTION_FAILED",
        metadata_json={"failure_permanent": True},
    )

    text = format_item_failure(item, [source])
    assert text.startswith("⚠️ Голосовое")
    assert "Не удалось распознать речь." in text
    assert "TRANSCRIPTION_FAILED" not in text
    assert "Можно повторить попытку." not in text
    callbacks = {
        button.callback_data
        for row in item_keyboard(item, [source]).inline_keyboard
        for button in row
    }
    assert "item:retry:1" not in callbacks


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
    assert "Приоритет:" not in text
    assert "82/100" not in text
    assert "Интерес: обычный" in text
    assert "Следующее действие:\nПосмотреть блок про tool orchestration" in text
    assert "Почему приоритет:" not in text
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


def test_today_uses_source_derived_title_instead_of_missing_analysis_placeholder():
    item = make_ready_item()
    item.title = None
    item.source_type = SourceType.WEB
    source = ItemSource(
        item_id=item.id,
        source_index=0,
        source_type=SourceType.WEB,
        source_url="https://www.avito.ru/item",
    )

    text = format_today([item], {item.id: [source]})
    assert "🎯 Сегодня" in text
    assert "🎯 avito.ru" in text
    assert "1." not in text
    assert "/100" not in text


def test_today_is_object_centric_and_keeps_only_useful_per_item_details():
    first = make_ready_item()
    first.title = "Docker release flow"
    first.next_action = "Check the dependency scan."
    first.estimated_action_minutes = 10
    second = make_ready_item()
    second.id = 2
    second.title = "Kotlin compiler changes"
    second.next_action = None
    second.summary = "A note about the new API."

    text = format_today([first, second])

    assert "Docker release flow" in text
    assert "Check the dependency scan." in text
    assert "≈ 10 минут" in text
    assert "Kotlin compiler changes" in text
    assert "A note about the new API." in text
    assert not any(term in text for term in ("1.", "2.", "1/2", "/100", "actionable", "Item"))


def test_manual_attention_and_snooze_copy_name_the_concrete_save():
    item = make_ready_item()
    item.title = "A saved title"
    item.summary = "Persisted content summary."
    attention = format_attention_item(2, 5, item, None)
    snooze = format_snooze_reminder(item)

    assert attention.startswith("🎯 A saved title")
    assert "2/5" not in attention
    assert "Persisted content summary." in attention
    assert snooze.startswith("⏰ Вы хотели вернуться к этому:")
    assert "A saved title" in snooze
    assert "Persisted content summary." in snooze


def test_category_format_stays_within_telegram_limit():
    text = format_categories(
        [(f"category-{index}-{'x' * 300}", index) for index in range(30)], page=3
    )
    assert len(text) <= 4096
    assert "Категории · 4" not in text
    assert "category-3" in text
    assert "— 3" not in text


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
