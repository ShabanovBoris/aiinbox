from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import ContentKind, ItemState, ItemType, ProcessingStatus, SourceType


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # Персональный профиль (Phase 8) — JSON в users; отдельные таблицы не нужны.
    profile_json: Mapped[dict | None] = mapped_column(JSON)
    # Настройки уведомлений хранятся отдельно от профиля: это операционные
    # предпочтения, а не семантический контекст анализа Item.
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC", nullable=False
    )
    settings_json: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    # Отдельная отметка включения digest отличает активацию от общего
    # updated_at: изменение профиля не должно отменять legitimate recovery.
    daily_digest_enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=func.now(), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Item(Base):
    # Идемпотентность Telegram-источника enforced на уровне БД, а не только логики:
    # повторный update не создаёт второй Item (PRODUCT_SPEC §13).
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("user_id", "telegram_message_id", "source_index", name="uq_items_source"),
        # ❌ Удалена уникальность Item по URL: Item теперь идентифицирует входящее
        # сообщение, а URL — дочерний ItemSource. Один URL в разных постах может
        # иметь разный контекст и поэтому не должен склеивать два Item.
        CheckConstraint("interest_level BETWEEN 1 AND 3", name="ck_items_interest_level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    source_index: Mapped[int] = mapped_column(default=0)

    processing_status: Mapped[ProcessingStatus] = mapped_column(
        SaEnum(ProcessingStatus, native_enum=False, length=16),
        default=ProcessingStatus.QUEUED,
        index=True,
    )
    # Пользовательский lifecycle не смешивается с processing_status (PRODUCT_SPEC §10).
    state: Mapped[ItemState] = mapped_column(
        SaEnum(ItemState, native_enum=False, length=16), default=ItemState.ACTIVE
    )
    source_type: Mapped[SourceType] = mapped_column(
        SaEnum(
            SourceType,
            native_enum=False,
            length=16,
            create_constraint=True,
            name="ck_items_source_type",
        )
    )
    # Совместимая presentation-проекция одиночного URL; canonical набор источников
    # нового Item хранится в item_sources.
    source_url: Mapped[str | None] = mapped_column(String(700))
    # file_id Telegram-файла для VOICE/AUDIO (скачивание на этапе extraction).
    source_file_id: Mapped[str | None] = mapped_column(String(200))
    # Длительность медиа-контента (voice/audio/video), секунд.
    content_duration_seconds: Mapped[int | None] = mapped_column()
    # Provenance остаётся свойством исходного envelope, а не новым SourceType:
    # Telegram-forward metadata хранится отдельно от бизнес-классификации Item.
    source_metadata_json: Mapped[dict | None] = mapped_column(JSON)

    # Текущий этап конвейера — позволяет понять, где обработка остановилась
    # при сбое (PRODUCT_SPEC §60, D-001 resumable).
    processing_stage: Mapped[str] = mapped_column(String(32), default="INGESTED")
    user_note: Mapped[str] = mapped_column(Text)

    # Результат анализа (заполняется пайплайном Phase 2); до анализа — NULL.
    title: Mapped[str | None] = mapped_column(String(300))
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    item_type: Mapped[ItemType | None] = mapped_column(
        SaEnum(ItemType, native_enum=False, length=16)
    )
    tags_json: Mapped[list | None] = mapped_column(JSON)
    importance: Mapped[float | None] = mapped_column(Float)
    urgency: Mapped[float | None] = mapped_column(Float)
    goal_fit: Mapped[float | None] = mapped_column(Float)
    long_term_value: Mapped[float | None] = mapped_column(Float)
    interest_fit: Mapped[float | None] = mapped_column(Float)
    estimated_action_minutes: Mapped[int | None] = mapped_column(Integer)
    priority_score: Mapped[int | None] = mapped_column(Integer, index=True)
    # Ручной интерес — отдельный пользовательский сигнал. Он не участвует в
    # PriorityEngine и потому не смешивается с model-derived interest_fit.
    interest_level: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    priority_reason: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    suggested_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    # Полнота анализа (ТЗ §24): не выдаём ложное ощущение полного визуального
    # анализа, если анализировался только транскрипт.
    analysis_completeness: Mapped[str | None] = mapped_column(String(32))

    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime)

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Content(Base):
    """Длинный контент отдельно от Item (ТЗ §46–48): исходный extracted text,
    транскрипты и т.п. переживают restart и позволяют retry без повторной работы."""

    __tablename__ = "contents"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    # Extracted content may belong to one concrete source inside a composite Item.
    # NULL means Item-level content such as Telegram message text or LLM chunk summaries.
    source_id: Mapped[int | None] = mapped_column(ForeignKey("item_sources.id"), index=True)
    kind: Mapped[ContentKind] = mapped_column(
        SaEnum(
            ContentKind,
            native_enum=False,
            length=16,
            create_constraint=True,
            name="ck_contents_kind",
        )
    )
    text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ItemSource(Base):
    """Durable independently extractable source inside one user-visible Item.

    Item owns lifecycle/analysis; ItemSource owns only extraction identity and
    outcome. This keeps one Telegram message as one Item while allowing each URL
    or media attachment to resume/fail independently before the common analysis.
    """

    __tablename__ = "item_sources"
    __table_args__ = (
        UniqueConstraint("item_id", "source_index", name="uq_item_sources_index"),
        CheckConstraint(
            "extraction_status IN ('PENDING', 'READY', 'FAILED')",
            name="ck_item_sources_extraction_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[SourceType] = mapped_column(
        SaEnum(
            SourceType,
            native_enum=False,
            length=16,
            create_constraint=True,
            name="ck_item_sources_source_type",
        )
    )
    source_url: Mapped[str | None] = mapped_column(String(700))
    source_file_id: Mapped[str | None] = mapped_column(String(200))
    content_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    extraction_status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    @property
    def failure_is_permanent(self) -> bool:
        """Keep retry policy durable with the source outcome across process restarts."""
        return bool((self.metadata_json or {}).get("failure_permanent"))

    @property
    def user_note(self) -> str:
        """Extractor compatibility: intent belongs to Item, never to one child source."""
        return ""


class Event(Base):
    """Auxiliary history; nullable callback keys deduplicate transport retries."""

    __tablename__ = "events"
    __table_args__ = (
        Index(
            "uq_events_user_idempotency_key",
            "user_id",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FeedbackCallbackReceipt(Base):
    """Remember correction callbacks whose no-op must not become a later mutation.

    This transport record stays separate from Event so a user selecting the
    already-current value does not create a misleading semantic correction.
    """

    __tablename__ = "feedback_callback_receipts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_feedback_callback_receipts_user_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Reminder(Base):
    """Durable notification claim/state used by the periodic reminder worker.

    The row is the restart-safe idempotency key: a digest is identified by its
    user, type and local-day schedule, while a snooze reminder is tied to one
    Item and its exact ``snoozed_until`` timestamp.
    """

    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "item_id", "type", "scheduled_at", name="uq_reminders_delivery"
        ),
        Index(
            "uq_reminders_daily_digest",
            "user_id",
            "type",
            "scheduled_at",
            unique=True,
            sqlite_where=text("type = 'DAILY_DIGEST'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProfileUpdateJob(Base):
    """Фоновое задание /profile_update: handler только ставит в очередь (быстрый
    ACK), worker выполняет LLM + merge; статус durable и переживает restart."""

    __tablename__ = "profile_update_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Delivery(Base):
    """Durable outbox for immediate Telegram delivery after business commits.

    READY/FAILED Item transitions and completed profile updates create this row
    in the same transaction as canonical state. Telegram delivery is therefore
    retryable after restart without changing Item/ProfileUpdateJob state back.
    """

    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "(item_id IS NOT NULL AND profile_update_job_id IS NULL) OR "
            "(item_id IS NULL AND profile_update_job_id IS NOT NULL)",
            name="ck_deliveries_single_source",
        ),
        UniqueConstraint("item_id", "type", name="uq_deliveries_item_type"),
        UniqueConstraint("profile_update_job_id", "type", name="uq_deliveries_profile_job_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    profile_update_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_update_jobs.id"), index=True
    )
    type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
