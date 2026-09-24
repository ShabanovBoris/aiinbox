from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, Field

from app.domain.enums import AttentionHookType, ItemType, MotivationKind, SourceType


@dataclass(frozen=True, slots=True)
class MotivationCandidate:
    """Immutable projection of a verified backlog fact into one short nudge.

    MotivationService owns this domain result; policy and delivery stay in
    ReminderWorker, so the facts cannot accidentally affect Item ranking.
    """

    kind: MotivationKind
    score: int
    facts: Mapping[str, int]
    template_id: str
    rendered_text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


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
    # Source-provided text/caption accompanying a primary URL/media source.
    # It is distinct from user_note so forwarded author text never becomes user intent.
    source_context: str | None = None
    metadata: dict[str, Any] = {}


class UserGoal(BaseModel):
    name: str
    weight: float = 1.0


class UserProfile(BaseModel):
    """Персональный контекст анализа. В Phase 2 — default-профиль;
    хранение и /profile_update приходят в Phase 8."""

    preferred_language: str = Field(
        default="ru",
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    )
    profession: str | None = None
    domains: list[str] = []
    goals: list[UserGoal] = []
    interests: list[str] = []
    constraints: dict[str, Any] = {}
    free_text: str | None = None


DEFAULT_PROFILE = UserProfile(
    preferred_language="ru",
    profession=None,
    domains=[],
    goals=[],
    interests=[],
    constraints={},
    free_text=None,
)


class ConstraintEntry(BaseModel):
    """Ключ-значение constraint: строгая форма для Structured Outputs
    (произвольный dict несовместим с additionalProperties=false)."""

    key: str = Field(min_length=1, max_length=100)
    value: str | float | bool | None = None


class ProfilePatch(BaseModel):
    """Частичное обновление профиля: отсутствующие поля не меняются (Phase 8).
    extra=forbid: незапрошенные LLM поля → INVALID_LLM_OUTPUT, а не молчаливое
    отбрасывание."""

    model_config = {"extra": "forbid"}

    preferred_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    )
    profession: str | None = None
    domains: list[str] | None = None
    goals: list[UserGoal] | None = None
    interests: list[str] | None = None
    constraints: list[ConstraintEntry] | None = None
    free_text: str | None = None


class AttentionHookCandidate(BaseModel):
    """Strict provider DTO; persisted-evidence checks belong to the application service."""

    model_config = {"extra": "forbid"}

    hook_type: AttentionHookType
    text: str = Field(min_length=1, max_length=320)
    evidence_excerpt: str = Field(min_length=1, max_length=300)
    source_content_id: int = Field(gt=0)


class AttentionHookGeneration(BaseModel):
    """Bounded structured output used only for one proactive reminder attempt."""

    model_config = {"extra": "forbid"}

    candidates: list[AttentionHookCandidate] = Field(default_factory=list, max_length=3)


@dataclass(frozen=True)
class AttentionHook:
    """Immutable application projection that keeps ORM rows out of Telegram presentation."""

    content_id: int
    item_id: int
    source_id: int | None
    hook_type: AttentionHookType
    text: str
    evidence_content_id: int
    evidence_excerpt: str
    generator_version: int


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
