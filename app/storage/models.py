from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import ItemState, ProcessingStatus, SourceType


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
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

    # Текущий этап конвейера — позволяет понять, где обработка остановилась
    # при сбое (PRODUCT_SPEC §60, D-001 resumable).
    processing_stage: Mapped[str] = mapped_column(String(32), default="INGESTED")
    user_note: Mapped[str] = mapped_column(Text)

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
