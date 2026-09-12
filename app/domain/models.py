from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import ItemType, SourceType


class NormalizedContent(BaseModel):
    """Единый формат после извлечения: LLM не знает деталей Telegram/httpx/yt-dlp.

    Все источники (text, web, voice, video) приводятся к этой модели и дальше
    идут по одному пайплайну (PRODUCT_SPEC §17).
    """

    source_type: SourceType
    title: str | None = None
    text: str
    url: str | None = None
    # Заметка пользователя — сильный сигнал намерения, обязателен анализатору (ТЗ §13)
    user_note: str | None = None
    author: str | None = None
    language: str | None = None
    duration_seconds: int | None = None
    metadata: dict[str, Any] = {}


class UserGoal(BaseModel):
    name: str
    weight: float = 1.0


class UserProfile(BaseModel):
    """Персональный контекст анализа. В Phase 2 — default-профиль;
    хранение и /profile_update приходят в Phase 8."""

    profession: str | None = None
    domains: list[str] = []
    goals: list[UserGoal] = []
    interests: list[str] = []
    constraints: dict[str, Any] = {}
    free_text: str | None = None


DEFAULT_PROFILE = UserProfile(
    profession=None,
    domains=[],
    goals=[],
    interests=[],
    constraints={},
    free_text=None,
)


class AnalysisResult(BaseModel):
    """Строгая схема ответа LLM; валидируется Pydantic до попадания в БД.

    extra="forbid": неожиданные поля от модели (например, самовольный
    priority_score) — это INVALID_LLM_OUTPUT, а не молчаливое отбрасывание.
    priority_score здесь НЕ существует: LLM даёт только факторы, итоговый
    приоритет считает детерминированный PriorityEngine (PRODUCT_SPEC §30, §37).
    """

    model_config = {"extra": "forbid"}

    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(max_length=1200)
    category: str = Field(min_length=1, max_length=100)
    item_type: ItemType
    tags: list[str] = Field(max_length=8, default=[])

    importance: float = Field(ge=0.0, le=1.0)
    urgency: float = Field(ge=0.0, le=1.0)
    goal_fit: float = Field(ge=0.0, le=1.0)
    long_term_value: float = Field(ge=0.0, le=1.0)
    interest_fit: float = Field(ge=0.0, le=1.0)

    estimated_action_minutes: int | None = Field(default=None, ge=0)
    next_action: str | None = Field(default=None, max_length=250)
    suggested_due_at: datetime | None = None

    priority_reason: str = Field(max_length=500)
    language: str = Field(min_length=2, max_length=16)
    confidence: float = Field(ge=0.0, le=1.0)
