"""Детерминированные фейки для тестов: только тестовое окружение, не production path."""

from app.domain.enums import ItemType, SourceType
from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.llm.base import LlmError


def make_analysis(**overrides) -> AnalysisResult:
    base = dict(
        title="Архитектура AI-агентов",
        summary="Разбор подходов к оркестрации агентов.",
        category="AI",
        item_type=ItemType.LEARN,
        tags=["ai", "agents"],
        importance=0.8,
        urgency=0.4,
        goal_fit=0.9,
        long_term_value=0.7,
        interest_fit=0.8,
        estimated_action_minutes=25,
        next_action="Посмотреть блок про tool orchestration",
        suggested_due_at=None,
        priority_reason="Сильно связано с профессиональными целями",
        language="ru",
        confidence=0.9,
    )
    base.update(overrides)
    return AnalysisResult(**base)


class FakeLlmProvider:
    """Подменяет LlmProvider: детерминированный результат или заданная ошибка."""

    def __init__(self, result: AnalysisResult | None = None, error: LlmError | None = None):
        self.result = result or make_analysis()
        self.error = error
        self.calls: list[tuple[NormalizedContent, UserProfile, list[str]]] = []

    async def analyze(
        self, content: NormalizedContent, profile: UserProfile, categories: list[str]
    ):
        self.calls.append((content, profile, list(categories)))
        if self.error is not None:
            raise self.error
        return self.result


class FakePipeline:
    """Минимальный пайплайн для тестов claim/failure воркера (без LLM)."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def run(self, session, item) -> None:
        if self.fail:
            raise RuntimeError("boom")
        item.title = "stub"
        item.processing_stage = "READY"


def make_content(text: str = "текст") -> NormalizedContent:
    return NormalizedContent(source_type=SourceType.TEXT, text=text)


def invalid_analysis_json() -> str:
    return json_invalid()


def json_invalid() -> str:
    import json

    # Нарушает схему: score вне диапазона, неизвестный item_type
    return json.dumps({"title": "x", "summary": "y", "goal_fit": 5.0, "item_type": "NOPE"})
