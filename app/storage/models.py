from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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
        # Дедупликация URL per-user на уровне БД; для TEXT-Item source_url NULL
        # (SQLite уникальность не действует на NULL-пары).
        UniqueConstraint("user_id", "source_url", name="uq_items_user_url"),
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
        SaEnum(SourceType, native_enum=False, length=16)
    )
    # Нормализованный URL для WEB-источников (дедупликация, Открыть, retry).
    source_url: Mapped[str | None] = mapped_column(String(700))
    # file_id Telegram-файла для VOICE/AUDIO (скачивание на этапе extraction).
    source_file_id: Mapped[str | None] = mapped_column(String(200))
    # Длительность медиа-контента (voice/audio/video), секунд.
    content_duration_seconds: Mapped[int | None] = mapped_column()

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
    priority_reason: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    suggested_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    # Полнота анализа (ТЗ §24): не выдаём ложное ощущение полного визуального
    # анализа, если анализировался только транскрипт.
    analysis_completeness: Mapped[str | None] = mapped_column(String(32))

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
    kind: Mapped[ContentKind] = mapped_column(SaEnum(ContentKind, native_enum=False, length=16))
    text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
