"""Strict HTTP request/response projections for PM-18's external contract."""

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.services.ask_inbox import MAX_ASK_QUESTION_CHARS

MAX_API_CAPTURE_TEXT_CHARS = 20_000
MAX_API_USER_NOTE_CHARS = 2_000
MAX_API_SEARCH_QUERY_CHARS = 500


class _WriteRequest(BaseModel):
    """Reject unknown write fields so clients cannot smuggle server-owned authority."""

    model_config = ConfigDict(extra="forbid")


class CaptureRequest(_WriteRequest):
    """Bound selected page/text input while keeping explicit intent separate."""

    text: str = Field(min_length=1, max_length=MAX_API_CAPTURE_TEXT_CHARS, strict=True)
    user_note: str | None = Field(default=None, max_length=MAX_API_USER_NOTE_CHARS, strict=True)

    @field_validator("text")
    @classmethod
    def require_meaningful_text(cls, value: str) -> str:
        """Reject whitespace-only saves without altering the user's captured source."""
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value


class AskRequest(_WriteRequest):
    """Bound a standalone question before it enters the durable AskWorker queue."""

    question: str = Field(min_length=1, max_length=MAX_ASK_QUESTION_CHARS, strict=True)

    @field_validator("question")
    @classmethod
    def require_meaningful_question(cls, value: str) -> str:
        """Keep the durable Ask contract aligned with the PM-13 question limit."""
        if not value.strip():
            raise ValueError("question must contain non-whitespace characters")
        return value


class SnoozeRequest(_WriteRequest):
    """Require an absolute instant so replaying a request never shifts its target."""

    snoozed_until: datetime

    @field_validator("snoozed_until")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Reject ambiguous local timestamps at the HTTP boundary."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snoozed_until must include a timezone")
        return value


class InterestRequest(_WriteRequest):
    """Accept only the three canonical manual-interest levels."""

    level: int = Field(ge=1, le=3, strict=True)


class SettingsPatchRequest(_WriteRequest):
    """Expose only mutable user-facing notification preferences."""

    timezone: str | None = Field(default=None, min_length=1, max_length=64, strict=True)
    daily_digest_enabled: bool | None = None
    daily_digest_time: str | None = Field(default=None, min_length=5, max_length=5, strict=True)
    quiet_hours_start: str | None = Field(default=None, min_length=5, max_length=5, strict=True)
    quiet_hours_end: str | None = Field(default=None, min_length=5, max_length=5, strict=True)
    attention_enabled: bool | None = None
    attention_intensity: int | None = Field(default=None, ge=1, le=5, strict=True)
    generic_motivation_enabled: bool | None = None

    @field_validator(
        "daily_digest_enabled",
        "attention_enabled",
        "generic_motivation_enabled",
        mode="before",
    )
    @classmethod
    def require_json_booleans(cls, value):
        """Prevent integer/string coercion from changing notification policy."""
        if value is not None and type(value) is not bool:
            raise ValueError("setting must be a JSON boolean")
        return value

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "SettingsPatchRequest":
        """Keep null from being confused with a request to clear a non-null setting."""
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("setting values cannot be null")
        return self


class APIErrorDetail(BaseModel):
    """Sanitized error metadata shared by controlled HTTP failure responses."""

    code: str
    message: str
    fields: list[str] | None = None


class APIErrorResponse(BaseModel):
    """Stable envelope that never includes request values or exception details."""

    error: APIErrorDetail


class HealthResponse(BaseModel):
    """Public liveness projection without database or configuration details."""

    status: Literal["ok"]


class CaptureAcceptedResponse(BaseModel):
    """Fast acknowledgement for an Item durably queued by external capture."""

    id: int
    processing_status: ProcessingStatus
    state: ItemState
    created_at: str


class ItemListEntryResponse(BaseModel):
    """Bounded Item fields safe for cursor browsing and recommendation lists."""

    id: int
    title: str
    summary: str | None
    category: str | None
    item_type: ItemType | None
    processing_status: ProcessingStatus
    state: ItemState
    priority_score: int | None
    interest_level: int
    created_at: str
    updated_at: str


class ItemSourceResponse(BaseModel):
    """Small source identity without extracted bodies, transcripts, or raw metadata."""

    id: int
    source_index: int
    source_type: SourceType
    source_url: str | None
    extraction_status: str
    error_code: str | None


class ItemDetailResponse(ItemListEntryResponse):
    """Owner-scoped detail projection with safe sources and no Content.text."""

    processing_stage: str
    source_type: SourceType
    source_url: str | None
    tags: list[str]
    priority_reason: str | None
    next_action: str | None
    estimated_action_minutes: int | None
    suggested_due_at: str | None
    completed_at: str | None
    archived_at: str | None
    snoozed_until: str | None
    error_code: str | None
    sources: list[ItemSourceResponse]


class ItemPageResponse(BaseModel):
    """One bounded page; the next cursor carries ordering only."""

    items: list[ItemListEntryResponse]
    next_cursor: str | None


class ItemListResponse(BaseModel):
    """Shared wrapper for existing Item read projections."""

    items: list[ItemListEntryResponse]


class AttentionItemResponse(ItemListEntryResponse):
    """PM-07 result with its existing explainable score components."""

    attention_score: int
    interest_adjustment: int
    age_bonus: float
    neglect_bonus: int
    due_bonus: int
    stale_important_bonus: int
    recent_show_penalty: int
    reminder_preference_penalty: int
    notification_fatigue_penalty: int


class AttentionResponse(BaseModel):
    """Read-only manual Attention preview."""

    items: list[AttentionItemResponse]


class CategoryCountResponse(BaseModel):
    """A bounded weekly aggregate paired with its canonical category label."""

    category: str
    count: int


class WeeklyFlowResponse(BaseModel):
    """Created/completed/archived facts from the current local-week window."""

    created: int
    completed: int
    archived: int
    net_change: int


class WeeklyBacklogResponse(BaseModel):
    """Current actionable backlog facts from the existing WeeklyReview service."""

    active_actionable: int
    high_priority: int
    high_interest: int
    stale: int
    old_important_unrevisited: int


class WeeklyReminderOutcomesResponse(BaseModel):
    """Observed Reminder outcomes; no new event or scheduler state is created."""

    sent: int
    opened: int
    snoozed: int
    done: int
    dismissed: int
    disliked: int


class WeeklyRecommendationResponse(BaseModel):
    """A concrete existing Item recommendation, not generated report prose."""

    kind: str
    item_id: int
    title: str
    estimated_action_minutes: int | None


class WeeklyResponse(BaseModel):
    """Structured, read-only projection of the existing PM-12 report."""

    flow: WeeklyFlowResponse
    backlog: WeeklyBacklogResponse
    created_categories: list[CategoryCountResponse]
    completed_categories: list[CategoryCountResponse]
    most_postponed: CategoryCountResponse | None
    strongest_progress: CategoryCountResponse | None
    reminder_outcomes: WeeklyReminderOutcomesResponse | None
    recommendations: list[WeeklyRecommendationResponse]


class AskAcceptedResponse(BaseModel):
    """202 response for the single durable HTTP Ask computation."""

    id: int
    status: Literal["PENDING", "RUNNING", "DONE", "FAILED"]


class AskReferenceResponse(BaseModel):
    """Citation display metadata resolved from persisted owner-scoped rows."""

    item_id: int
    source_id: int | None
    title: str
    source_type: SourceType | None
    source_url: str | None


class AskPendingResponse(BaseModel):
    """Poll response while the shared AskWorker owns the request."""

    id: int
    status: Literal["PENDING", "RUNNING"]


class AskDoneResponse(BaseModel):
    """Transient validated answer with database-resolved citations."""

    id: int
    status: Literal["DONE"]
    answer: str
    references: list[AskReferenceResponse]
    expires_at: str


class AskFailedResponse(BaseModel):
    """Controlled failure state without provider exception text."""

    id: int
    status: Literal["FAILED"]
    error: APIErrorDetail


AskPollResponse = Annotated[
    Union[AskPendingResponse, AskDoneResponse, AskFailedResponse],
    Field(discriminator="status"),
]


class NotificationSettingsResponse(BaseModel):
    """Allowlisted settings only; operator and provider secrets are not fields."""

    timezone: str
    daily_digest_enabled: bool
    daily_digest_time: str
    quiet_hours_start: str
    quiet_hours_end: str
    attention_enabled: bool
    attention_intensity: int
    generic_motivation_enabled: bool
