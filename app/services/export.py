"""Portable, user-scoped export projections and durable job finalization.

The service reads canonical rows into detached DTOs, closes SQLite before any
archive work, and records the resulting file through the existing delivery outbox.
"""

import asyncio
import json
import logging
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.enums import ContentKind, ItemType, MotivationKind
from app.domain.models import UserProfile
from app.services.notifications import settings_for
from app.storage.models import (
    Content,
    Delivery,
    Event,
    ExportJob,
    Item,
    ItemSource,
    Reminder,
    User,
)

log = logging.getLogger(__name__)

COMPACT = "COMPACT"
FULL = "FULL"
EXPORT_FILE = "EXPORT_FILE"
EXPORT_FAILED = "EXPORT_FAILED"
_MODES = frozenset({COMPACT, FULL})
_PRIMARY_CONTENT_KINDS = (
    ContentKind.USER_TEXT,
    ContentKind.WEB_TEXT,
    ContentKind.DOCUMENT_TEXT,
    ContentKind.TRANSCRIPT,
    ContentKind.VISUAL_NOTES,
    ContentKind.DESCRIPTION,
)
_SETTINGS_ALLOWLIST = (
    "daily_digest_enabled",
    "daily_digest_time",
    "quiet_hours_start",
    "quiet_hours_end",
    "attention_enabled",
    "attention_intensity",
    "generic_motivation_enabled",
)
_FORWARD_ORIGIN_TYPES = frozenset({"user", "hidden_user", "chat", "channel"})
_REMINDER_EVENT_TYPES = frozenset(
    {
        "REMINDER_SENT",
        "REMINDER_OPENED",
        "REMINDER_SNOOZED",
        "REMINDER_DONE",
        "REMINDER_DISMISSED",
        "REMINDER_DISLIKED",
    }
)
_EXPORT_NAME = re.compile(r"^aiinbox-export-[1-9][0-9]*-[0-9a-f]{32}\.zip$")
_TEMP_NAME = re.compile(r"^aiinbox-export-[1-9][0-9]*-[a-zA-Z0-9_-]+\.tmp$")
_ARCHIVE_FILES = (
    "manifest.json",
    "README.md",
    "profile.json",
    "settings.json",
    "items.jsonl",
    "sources.jsonl",
    "events.jsonl",
    "reminders.jsonl",
    "items.md",
    "contents.jsonl",
)


class ExportError(Exception):
    """A controlled generation failure that can be persisted and delivered safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExportItem:
    """Detached Item allowlist; this is the portable schema, not an ORM snapshot."""

    id: int
    processing_status: str
    state: str
    source_type: str
    source_url: str | None
    source_provenance_json: str | None
    title: str | None
    summary: str | None
    category: str | None
    item_type: str | None
    tags: tuple[str, ...]
    importance: float | None
    urgency: float | None
    goal_fit: float | None
    long_term_value: float | None
    interest_fit: float | None
    estimated_action_minutes: int | None
    priority_score: int | None
    interest_level: int
    priority_reason: str | None
    next_action: str | None
    suggested_due_at: datetime | None
    language: str | None
    confidence: float | None
    analysis_completeness: str | None
    created_at: datetime | None
    updated_at: datetime | None
    completed_at: datetime | None
    archived_at: datetime | None
    snoozed_until: datetime | None
    user_note: str


@dataclass(frozen=True, slots=True)
class ExportSource:
    """Detached source identity and user-visible extraction provenance."""

    id: int
    item_id: int
    source_index: int
    source_type: str
    source_url: str | None
    content_duration_seconds: int | None
    extraction_status: str
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportContent:
    """One primary persisted evidence row allowed only in full exports."""

    id: int
    item_id: int
    source_id: int | None
    kind: str
    text: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportEvent:
    """Sanitized user history with only portable Item/Reminder references."""

    id: int
    event_type: str
    item_id: int | None
    reminder_id: int | None
    created_at: datetime | None
    payload_json: str


@dataclass(frozen=True, slots=True)
class ExportReminder:
    """Portable reminder history without scheduler claims or private payloads."""

    id: int
    item_id: int | None
    reminder_type: str
    status: str
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportSnapshot:
    """Consistent detached projection that lets the archive phase release SQLite."""

    user_id: int
    mode: str
    generated_at: datetime
    profile_json: str
    settings_json: str
    items: tuple[ExportItem, ...]
    sources: tuple[ExportSource, ...]
    contents: tuple[ExportContent, ...]
    events: tuple[ExportEvent, ...]
    reminders: tuple[ExportReminder, ...]


def _json_dumps(value: object) -> str:
    """Serialize portable values with stable keys and no non-standard NaN values."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _datetime_iso(value: datetime | None) -> str | None:
    """Make naive SQLite UTC values explicit and normalize aware values to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _profile_projection(profile_json: dict | None) -> dict:
    """Expose the same validated semantic profile the application uses, without seeding DB."""
    try:
        profile = UserProfile.model_validate(profile_json or {})
    except ValidationError:
        profile = UserProfile()
    return profile.model_dump(mode="json")


def _forward_provenance(metadata: dict | None) -> dict | None:
    """Copy only Telegram's persisted forward-origin fields; never dump opaque metadata."""
    if not isinstance(metadata, dict):
        return None
    result: dict[str, object] = {}
    if metadata.get("forwarded") is True:
        result["forwarded"] = True
    origin_type = metadata.get("forward_origin_type")
    if isinstance(origin_type, str) and origin_type in _FORWARD_ORIGIN_TYPES:
        result["forward_origin_type"] = origin_type
    name = metadata.get("forward_source_name")
    if isinstance(name, str) and name.strip():
        result["forward_source_name"] = " ".join(name.split())[:120]
    username = metadata.get("forward_source_username")
    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
        result["forward_source_username"] = username
    message_id = metadata.get("forward_message_id")
    if type(message_id) is int and message_id > 0 and origin_type == "channel":
        result["forward_message_id"] = message_id
    original_sent_at = metadata.get("original_sent_at")
    if isinstance(original_sent_at, str):
        try:
            parsed = datetime.fromisoformat(original_sent_at.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            result["original_sent_at"] = _datetime_iso(parsed)
    return result or None


def _event_payload(event_type: str, payload: dict | None) -> dict:
    """Keep only bounded, event-specific history fields produced by current services."""
    if not isinstance(payload, dict):
        return {}

    result: dict[str, object] = {}
    if event_type in {"CATEGORY_CORRECTED", "TYPE_CORRECTED"}:
        for key in ("from", "to"):
            value = payload.get(key)
            if value is None or isinstance(value, str):
                result[key] = value[:100] if isinstance(value, str) else None
        if payload.get("source") == "telegram":
            result["source"] = "telegram"
    elif event_type == "INTEREST_CHANGED":
        for key in ("from", "to"):
            value = payload.get(key)
            if type(value) is int and 1 <= value <= 3:
                result[key] = value
        if payload.get("source") == "telegram":
            result["source"] = "telegram"
    elif event_type == "SNOOZED":
        value = payload.get("snoozed_until")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                result["snoozed_until"] = _datetime_iso(parsed)
    elif event_type in {
        "USEFUL",
        "NOT_INTERESTING",
        "SUMMARY_REPORTED_WRONG",
        "PRIORITY_HIGHER",
        "PRIORITY_LOWER",
    }:
        if payload.get("source") == "telegram":
            result["source"] = "telegram"
        if payload.get("surface") == "item_result":
            result["surface"] = "item_result"
        score = payload.get("priority_score_at_feedback")
        if (
            event_type in {"PRIORITY_HIGHER", "PRIORITY_LOWER"}
            and type(score) is int
            and 0 <= score <= 100
        ):
            result["priority_score_at_feedback"] = score
    elif event_type == "ATTENTION_SHOWN":
        if payload.get("source") == "proactive_attention":
            result["source"] = "proactive_attention"
        reminder_id = payload.get("reminder_id")
        if type(reminder_id) is int and reminder_id > 0:
            result["reminder_id"] = reminder_id
    elif event_type in _REMINDER_EVENT_TYPES:
        reminder_type = payload.get("reminder_type")
        if isinstance(reminder_type, str) and reminder_type in {
            "DAILY_DIGEST",
            "SNOOZE_RESURFACE",
            "PROACTIVE_ATTENTION",
            "MOTIVATION_NUDGE",
        }:
            result["reminder_type"] = reminder_type
        policy_level = payload.get("policy_level")
        if type(policy_level) is int and 1 <= policy_level <= 5:
            result["policy_level"] = policy_level
        category = payload.get("category")
        if isinstance(category, str) and category.strip():
            result["category"] = " ".join(category.split())[:100]
        item_type = payload.get("item_type")
        if isinstance(item_type, str) and item_type in {value.value for value in ItemType}:
            result["item_type"] = item_type
        motivation_kind = payload.get("motivation_kind")
        if motivation_kind is None:
            motivation_kind = payload.get("kind")
        if isinstance(motivation_kind, str) and motivation_kind in {
            value.value for value in MotivationKind
        }:
            result["motivation_kind"] = motivation_kind
        template_id = payload.get("template_id")
        if isinstance(template_id, str) and template_id:
            result["template_id"] = template_id[:96]
        for key in ("hook_content_id", "source_id"):
            value = payload.get(key)
            if type(value) is int and value > 0:
                result[key] = value
        local_date = payload.get("local_date")
        if isinstance(local_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date):
            result["local_date"] = local_date
    return result


def _item_record(item: ExportItem) -> dict:
    """Project one detached Item into the versioned portable JSONL row shape."""
    return {
        "id": item.id,
        "processing_status": item.processing_status,
        "state": item.state,
        "source_type": item.source_type,
        "source_url": item.source_url,
        "source_provenance": json.loads(item.source_provenance_json)
        if item.source_provenance_json
        else None,
        "title": item.title,
        "summary": item.summary,
        "category": item.category,
        "item_type": item.item_type,
        "tags": list(item.tags),
        "importance": item.importance,
        "urgency": item.urgency,
        "goal_fit": item.goal_fit,
        "long_term_value": item.long_term_value,
        "interest_fit": item.interest_fit,
        "estimated_action_minutes": item.estimated_action_minutes,
        "priority_score": item.priority_score,
        "interest_level": item.interest_level,
        "priority_reason": item.priority_reason,
        "next_action": item.next_action,
        "suggested_due_at": _datetime_iso(item.suggested_due_at),
        "language": item.language,
        "confidence": item.confidence,
        "analysis_completeness": item.analysis_completeness,
        "created_at": _datetime_iso(item.created_at),
        "updated_at": _datetime_iso(item.updated_at),
        "completed_at": _datetime_iso(item.completed_at),
        "archived_at": _datetime_iso(item.archived_at),
        "snoozed_until": _datetime_iso(item.snoozed_until),
        "user_note": item.user_note,
    }


def _source_record(source: ExportSource) -> dict:
    """Project a child source while omitting Telegram file identifiers and retry errors."""
    return {
        "id": source.id,
        "item_id": source.item_id,
        "source_index": source.source_index,
        "source_type": source.source_type,
        "source_url": source.source_url,
        "content_duration_seconds": source.content_duration_seconds,
        "extraction_status": source.extraction_status,
        "created_at": _datetime_iso(source.created_at),
        "updated_at": _datetime_iso(source.updated_at),
    }


def _content_record(content: ExportContent) -> dict:
    """Project primary persisted evidence without extraction metadata or checkpoints."""
    return {
        "id": content.id,
        "item_id": content.item_id,
        "source_id": content.source_id,
        "kind": content.kind,
        "text": content.text,
        "created_at": _datetime_iso(content.created_at),
    }


def _event_record(event: ExportEvent) -> dict:
    """Project a sanitized user-history row with explicit nullable references."""
    return {
        "id": event.id,
        "event_type": event.event_type,
        "item_id": event.item_id,
        "reminder_id": event.reminder_id,
        "created_at": _datetime_iso(event.created_at),
        "payload": json.loads(event.payload_json),
    }


def _reminder_record(reminder: ExportReminder) -> dict:
    """Project portable Reminder history without its private worker payload or claim."""
    return {
        "id": reminder.id,
        "item_id": reminder.item_id,
        "type": reminder.reminder_type,
        "status": reminder.status,
        "scheduled_at": _datetime_iso(reminder.scheduled_at),
        "sent_at": _datetime_iso(reminder.sent_at),
        "created_at": _datetime_iso(reminder.created_at),
    }


def _jsonl(records, projector) -> str:
    """Write stable one-object-per-line data after the database snapshot is detached."""
    return "".join(_json_dumps(projector(record)) + "\n" for record in records)


def _markdown_text(value: str | None, limit: int) -> str:
    """Flatten untrusted display text and escape Markdown structure without altering JSON."""
    if not value:
        return ""
    text = " ".join(value.split())
    clipped = len(text) > limit
    text = text[:limit]
    for marker in ("\\", "`", "*", "_", "[", "]", "(", ")", "#", ">", "|"):
        text = text.replace(marker, "\\" + marker)
    return text + ("…" if clipped else "")


def _items_markdown(snapshot: ExportSnapshot) -> str:
    """Create a readable short index; full persisted evidence remains in contents.jsonl."""
    sources_by_item: dict[int, list[ExportSource]] = {}
    for source in snapshot.sources:
        sources_by_item.setdefault(source.item_id, []).append(source)
    blocks = ["# AIInbox — сохранённые Items", ""]
    for item in snapshot.items:
        title = _markdown_text(item.title or "Без названия", 200)
        blocks.extend(
            [
                f"## {title}",
                "",
                f"- ID: {item.id}",
                f"- Состояние: {item.state} / {item.processing_status}",
                f"- Тип: {item.item_type or item.source_type}",
                f"- Категория: {_markdown_text(item.category, 100)}",
                f"- Приоритет: {item.priority_score if item.priority_score is not None else '—'}",
                f"- Интерес: {item.interest_level}",
                f"- Создано: {_datetime_iso(item.created_at) or '—'}",
            ]
        )
        source_list = sources_by_item.get(item.id, [])
        if source_list:
            source_summary = ", ".join(
                f"{source.id} ({source.source_type})" for source in source_list
            )
            blocks.append(f"- Источники: {source_summary}")
        summary = _markdown_text(item.summary, 2000)
        if summary:
            blocks.extend(["", summary])
        note = _markdown_text(item.user_note, 1000)
        if note:
            blocks.extend(["", "### Моя заметка", "", note])
        if snapshot.mode == FULL and snapshot.contents:
            blocks.extend(["", "Полный текст и транскрипты находятся в `contents.jsonl`."])
        blocks.append("")
    return "\n".join(blocks)


def _archive_readme(snapshot: ExportSnapshot) -> str:
    """Explain how to read this portable format and its explicit privacy boundary."""
    mode = "полный" if snapshot.mode == FULL else "компактный"
    contents_note = (
        "`contents.jsonl` содержит первичный сохранённый текст, документы, транскрипты, "
        "визуальные заметки и описания."
        if snapshot.mode == FULL
        else "Компактный режим не содержит длинный сохранённый текст и транскрипты."
    )
    return (
        "# Экспорт AIInbox\n\n"
        f"Это {mode} пользовательский экспорт формата `aiinbox-export`, версия 1. "
        "Экспорт предназначен для чтения вне AIInbox и не является SQLite backup.\n\n"
        "## Файлы\n\n"
        "- `manifest.json` — версия формата, время создания и counts.\n"
        "- `profile.json` и `settings.json` — профиль и effective пользовательские настройки.\n"
        "- `items.jsonl`, `sources.jsonl`, `events.jsonl`, `reminders.jsonl` — данные, "
        "по одной JSON-записи на строку.\n"
        "- `items.md` — краткий человекочитаемый обзор Items.\n"
        f"- {contents_note}\n\n"
        "## Связи и даты\n\n"
        "`item_id` связывает источники, содержимое, события и напоминания с Item; "
        "`source_id` связывает содержимое с ItemSource. Все даты имеют ISO-8601 формат "
        "UTC с суффиксом `Z`.\n\n"
        "## Граница экспорта\n\n"
        "Экспорт содержит только явно выбранные пользовательские поля. Он не включает "
        "секреты, Telegram transport IDs/file IDs, worker jobs, outbox, callback receipts, "
        "поисковые индексы, checkpoint/derived Content или временные Ask-ответы. "
        "Backup восстанавливает AIInbox; экспорт даёт переносимые данные. Экспорт не "
        "является импортом и не предназначен для восстановления приложения.\n"
    )


def _archive_files(snapshot: ExportSnapshot) -> dict[str, str]:
    """Materialize only fixed-name archive members from the detached explicit projection."""
    counts = {
        "items": len(snapshot.items),
        "sources": len(snapshot.sources),
        "events": len(snapshot.events),
        "reminders": len(snapshot.reminders),
        "contents": len(snapshot.contents),
    }
    files = {
        "manifest.json": _json_dumps(
            {
                "format": "aiinbox-export",
                "version": 1,
                "generated_at": _datetime_iso(snapshot.generated_at),
                "mode": snapshot.mode.casefold(),
                "user": {"id": snapshot.user_id},
                "counts": counts,
            }
        )
        + "\n",
        "README.md": _archive_readme(snapshot),
        "profile.json": snapshot.profile_json + "\n",
        "settings.json": snapshot.settings_json + "\n",
        "items.jsonl": _jsonl(snapshot.items, _item_record),
        "sources.jsonl": _jsonl(snapshot.sources, _source_record),
        "events.jsonl": _jsonl(snapshot.events, _event_record),
        "reminders.jsonl": _jsonl(snapshot.reminders, _reminder_record),
        "items.md": _items_markdown(snapshot),
    }
    if snapshot.mode == FULL:
        files["contents.jsonl"] = _jsonl(snapshot.contents, _content_record)
    return files


def ensure_export_directory(export_dir: str | Path) -> Path:
    """Create the separately configured private artifact directory with owner-only access."""
    path = Path(export_dir)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise OSError("EXPORT_DIR must be a real directory")
    path.chmod(0o700)
    return path.resolve()


def resolve_export_artifact(export_dir: str | Path, artifact_name: object) -> Path:
    """Resolve only application-generated leaf names and reject traversal or symlinks."""
    if not isinstance(artifact_name, str) or _EXPORT_NAME.fullmatch(artifact_name) is None:
        raise ValueError("invalid export artifact name")
    root_path = Path(export_dir)
    if root_path.is_symlink():
        raise ValueError("export directory must not be a symlink")
    root = root_path.resolve()
    if not root.is_dir():
        raise ValueError("export directory is unavailable")
    path = root / artifact_name
    if path.name != artifact_name or path.is_symlink():
        raise ValueError("export artifact path is unsafe")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError("export artifact is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("export artifact is not a private regular file")
    if path.resolve().parent != root:
        raise ValueError("export artifact escaped its directory")
    return path


def remove_export_artifact(export_dir: str | Path, artifact_name: object) -> bool:
    """Remove one already-scoped archive after its durable outbox row becomes SENT."""
    path = resolve_export_artifact(export_dir, artifact_name)
    path.unlink()
    return True


def _write_archive(snapshot: ExportSnapshot, job_id: int, export_dir: Path) -> dict:
    """Write, fsync, atomically publish, and validate one bounded ZIP artifact."""
    from app.services.delivery import TELEGRAM_MAX_UPLOAD_BYTES

    export_dir = ensure_export_directory(export_dir)
    artifact_name = f"aiinbox-export-{job_id}-{uuid4().hex}.zip"
    final_path = export_dir / artifact_name
    fd, temporary_name = tempfile.mkstemp(
        prefix=f"aiinbox-export-{job_id}-", suffix=".tmp", dir=export_dir
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w+b") as archive_file:
            files = _archive_files(snapshot)
            names = [name for name in _ARCHIVE_FILES if name in files]
            with zipfile.ZipFile(
                archive_file, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                for name in names:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(info, files[name].encode("utf-8"))
            archive_file.flush()
            os.fsync(archive_file.fileno())
        os.replace(temporary_path, final_path)
        directory_fd = os.open(export_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        metadata = final_path.stat()
        if (
            final_path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or final_path.resolve().parent != export_dir.resolve()
        ):
            raise ExportError("EXPORT_FAILED", "Export archive failed path validation")
        if metadata.st_size > TELEGRAM_MAX_UPLOAD_BYTES:
            raise ExportError("EXPORT_TOO_LARGE", "Export archive exceeds Telegram's upload limit")
        with zipfile.ZipFile(final_path) as archive:
            expected = set(names)
            if not expected.issubset(archive.namelist()) or archive.testzip() is not None:
                raise ExportError("EXPORT_FAILED", "Export archive failed integrity validation")
        final_path.chmod(0o600)
        return {
            "artifact_name": artifact_name,
            "mode": snapshot.mode,
            "size_bytes": metadata.st_size,
        }
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise


async def enqueue_export(
    session_factory: async_sessionmaker,
    *,
    telegram_user_id: int,
    telegram_message_id: int,
    chat_id: int,
    mode: str,
    default_timezone: str = "UTC",
) -> ExportJob:
    """Persist one fast Telegram request; its message identity absorbs transport redelivery."""
    from app.services.ingestion import get_or_create_user

    normalized_mode = mode.upper()
    if normalized_mode not in _MODES:
        raise ValueError("export mode must be compact or full")
    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=default_timezone,
        )
        result = await session.execute(
            sqlite_insert(ExportJob)
            .values(
                user_id=user.id,
                telegram_message_id=telegram_message_id,
                mode=normalized_mode,
                status="PENDING",
            )
            .on_conflict_do_nothing(
                index_elements=[ExportJob.user_id, ExportJob.telegram_message_id]
            )
            .returning(ExportJob.id)
        )
        job_id = result.scalar_one_or_none()
        if job_id is None:
            job = await session.scalar(
                select(ExportJob).where(
                    ExportJob.user_id == user.id,
                    ExportJob.telegram_message_id == telegram_message_id,
                )
            )
        else:
            job = await session.get(ExportJob, job_id)
        await session.commit()
        log.info("export job queued id=%s user_id=%s mode=%s", job.id, user.id, normalized_mode)
        return job


async def claim_oldest_export_job(session_factory: async_sessionmaker) -> ExportJob | None:
    """Atomically move the oldest PENDING request to RUNNING before any archive work."""
    async with session_factory() as session:
        result = await session.execute(
            update(ExportJob)
            .where(
                ExportJob.id
                == select(ExportJob.id)
                .where(ExportJob.status == "PENDING")
                .order_by(ExportJob.created_at, ExportJob.id)
                .limit(1)
                .scalar_subquery(),
                ExportJob.status == "PENDING",
            )
            .values(status="RUNNING", updated_at=func.now())
            .returning(ExportJob.id)
        )
        job_id = result.scalar_one_or_none()
        await session.commit()
        return await session.get(ExportJob, job_id) if job_id is not None else None


async def requeue_running_export_jobs(session_factory: async_sessionmaker) -> int:
    """Return interrupted generations to the durable queue at application startup."""
    async with session_factory() as session:
        result = await session.execute(
            update(ExportJob).where(ExportJob.status == "RUNNING").values(status="PENDING")
        )
        await session.commit()
        if result.rowcount:
            log.warning("requeued RUNNING export jobs count=%s", result.rowcount)
        return result.rowcount


async def build_export_snapshot(
    session_factory: async_sessionmaker,
    *,
    user_id: int,
    mode: str,
    max_content_chars: int,
) -> ExportSnapshot:
    """Read an owner-scoped consistent snapshot and detach explicit portable DTOs."""
    if mode not in _MODES:
        raise ExportError("EXPORT_FAILED", "Export job has an unsupported mode")
    async with session_factory() as session:
        async with session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise ExportError("EXPORT_FAILED", "Export owner is unavailable")
            items = list(
                (
                    await session.scalars(
                        select(Item).where(Item.user_id == user_id).order_by(Item.id)
                    )
                ).all()
            )
            item_ids = {item.id for item in items}
            sources = list(
                (
                    await session.scalars(
                        select(ItemSource)
                        .join(Item, Item.id == ItemSource.item_id)
                        .where(Item.user_id == user_id)
                        .order_by(ItemSource.item_id, ItemSource.source_index, ItemSource.id)
                    )
                ).all()
            )
            source_to_item = {source.id: source.item_id for source in sources}
            reminders = list(
                (
                    await session.scalars(
                        select(Reminder)
                        .where(Reminder.user_id == user_id)
                        .order_by(Reminder.created_at, Reminder.id)
                    )
                ).all()
            )
            reminder_ids = {reminder.id for reminder in reminders}
            reminder_by_id = {reminder.id: reminder for reminder in reminders}
            for reminder in reminders:
                if reminder.item_id is not None and reminder.item_id not in item_ids:
                    raise ExportError(
                        "EXPORT_INVALID_SNAPSHOT", "Reminder has a foreign Item reference"
                    )

            events = list(
                (
                    await session.scalars(
                        select(Event)
                        .where(Event.user_id == user_id)
                        .order_by(Event.created_at, Event.id)
                    )
                ).all()
            )
            for event in events:
                if event.item_id is not None and event.item_id not in item_ids:
                    raise ExportError(
                        "EXPORT_INVALID_SNAPSHOT", "Event has a foreign Item reference"
                    )
                if event.reminder_id is not None and event.reminder_id not in reminder_ids:
                    raise ExportError(
                        "EXPORT_INVALID_SNAPSHOT", "Event has a foreign Reminder reference"
                    )
                if event.reminder_id is not None and event.item_id is not None:
                    if reminder_by_id[event.reminder_id].item_id != event.item_id:
                        raise ExportError(
                            "EXPORT_INVALID_SNAPSHOT",
                            "Event Item and Reminder references do not match",
                        )

            hook_content_rows = await session.execute(
                select(Content.id, Content.item_id)
                .join(Item, Item.id == Content.item_id)
                .where(Item.user_id == user_id, Content.kind == ContentKind.ATTENTION_HOOK)
            )
            hook_content_to_item = dict(hook_content_rows.all())

            content_rows: list[Content] = []
            if mode == FULL and item_ids:
                total_chars = await session.scalar(
                    select(func.coalesce(func.sum(func.length(Content.text)), 0))
                    .join(Item, Item.id == Content.item_id)
                    .where(Item.user_id == user_id, Content.kind.in_(_PRIMARY_CONTENT_KINDS))
                )
                if total_chars > max_content_chars:
                    raise ExportError(
                        "EXPORT_CONTENT_TOO_LARGE",
                        "Full export exceeds the configured content safety limit",
                    )
                content_rows = list(
                    (
                        await session.scalars(
                            select(Content)
                            .join(Item, Item.id == Content.item_id)
                            .where(
                                Item.user_id == user_id,
                                Content.kind.in_(_PRIMARY_CONTENT_KINDS),
                            )
                            .order_by(Content.item_id, Content.source_id, Content.id)
                        )
                    ).all()
                )
                for content in content_rows:
                    if (
                        content.source_id is not None
                        and source_to_item.get(content.source_id) != content.item_id
                    ):
                        raise ExportError(
                            "EXPORT_INVALID_SNAPSHOT",
                            "Content source does not belong to its owning Item",
                        )

            item_projection = tuple(
                ExportItem(
                    id=item.id,
                    processing_status=item.processing_status.value,
                    state=item.state.value,
                    source_type=item.source_type.value,
                    source_url=item.source_url,
                    source_provenance_json=(
                        _json_dumps(provenance)
                        if (provenance := _forward_provenance(item.source_metadata_json))
                        else None
                    ),
                    title=item.title,
                    summary=item.summary,
                    category=item.category,
                    item_type=item.item_type.value if item.item_type else None,
                    tags=tuple(tag for tag in (item.tags_json or []) if isinstance(tag, str)),
                    importance=item.importance,
                    urgency=item.urgency,
                    goal_fit=item.goal_fit,
                    long_term_value=item.long_term_value,
                    interest_fit=item.interest_fit,
                    estimated_action_minutes=item.estimated_action_minutes,
                    priority_score=item.priority_score,
                    interest_level=item.interest_level,
                    priority_reason=item.priority_reason,
                    next_action=item.next_action,
                    suggested_due_at=item.suggested_due_at,
                    language=item.language,
                    confidence=item.confidence,
                    analysis_completeness=item.analysis_completeness,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    completed_at=item.completed_at,
                    archived_at=item.archived_at,
                    snoozed_until=item.snoozed_until,
                    user_note=item.user_note,
                )
                for item in items
            )
            source_projection = tuple(
                ExportSource(
                    id=source.id,
                    item_id=source.item_id,
                    source_index=source.source_index,
                    source_type=source.source_type.value,
                    source_url=source.source_url,
                    content_duration_seconds=source.content_duration_seconds,
                    extraction_status=source.extraction_status,
                    created_at=source.created_at,
                    updated_at=source.updated_at,
                )
                for source in sources
            )
            content_projection = tuple(
                ExportContent(
                    id=content.id,
                    item_id=content.item_id,
                    source_id=content.source_id,
                    kind=content.kind.value,
                    text=content.text,
                    created_at=content.created_at,
                )
                for content in content_rows
            )
            event_projection_rows = []
            for event in events:
                payload = _event_payload(event.event_type, event.payload_json)
                reminder_id = payload.get("reminder_id")
                if type(reminder_id) is int and reminder_id not in reminder_ids:
                    payload.pop("reminder_id", None)
                owner_item_id = event.item_id
                if owner_item_id is None and event.reminder_id is not None:
                    owner_item_id = reminder_by_id[event.reminder_id].item_id
                source_id = payload.get("source_id")
                if type(source_id) is int and source_to_item.get(source_id) != owner_item_id:
                    payload.pop("source_id", None)
                hook_content_id = payload.get("hook_content_id")
                if (
                    type(hook_content_id) is int
                    and hook_content_to_item.get(hook_content_id) != owner_item_id
                ):
                    payload.pop("hook_content_id", None)
                event_projection_rows.append(
                    ExportEvent(
                        id=event.id,
                        event_type=event.event_type,
                        item_id=event.item_id,
                        reminder_id=event.reminder_id,
                        created_at=event.created_at,
                        payload_json=_json_dumps(payload),
                    )
                )
            event_projection = tuple(event_projection_rows)
            reminder_projection = tuple(
                ExportReminder(
                    id=reminder.id,
                    item_id=reminder.item_id,
                    reminder_type=reminder.type,
                    status=reminder.status,
                    scheduled_at=reminder.scheduled_at,
                    sent_at=reminder.sent_at,
                    created_at=reminder.created_at,
                )
                for reminder in reminders
            )
            settings = settings_for(user)
            portable_settings = {
                "timezone": user.timezone,
                **{key: settings[key] for key in _SETTINGS_ALLOWLIST},
            }
            return ExportSnapshot(
                user_id=user_id,
                mode=mode,
                generated_at=datetime.now(UTC),
                profile_json=_json_dumps(_profile_projection(user.profile_json)),
                settings_json=_json_dumps(portable_settings),
                items=item_projection,
                sources=source_projection,
                contents=content_projection,
                events=event_projection,
                reminders=reminder_projection,
            )


class ExportService:
    """Own durable export generation and atomically hand its artifact to Delivery."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        export_dir: str | Path,
        *,
        max_content_chars: int,
    ):
        self.session_factory = session_factory
        self.export_dir = ensure_export_directory(export_dir)
        self.max_content_chars = max_content_chars

    async def process(self, job: ExportJob) -> None:
        """Build outside the Telegram handler, then commit DONE with one file delivery."""
        from app.services.delivery import enqueue_export_delivery

        snapshot = await build_export_snapshot(
            self.session_factory,
            user_id=job.user_id,
            mode=job.mode,
            max_content_chars=self.max_content_chars,
        )
        artifact = await asyncio.to_thread(_write_archive, snapshot, job.id, self.export_dir)
        should_deliver = False
        async with self.session_factory() as session:
            current = await session.get(ExportJob, job.id)
            if current is None or current.status != "RUNNING":
                await session.rollback()
            else:
                current.status = "DONE"
                current.error_code = None
                current.error_message = None
                await enqueue_export_delivery(
                    session,
                    user_id=current.user_id,
                    export_job_id=current.id,
                    delivery_type=EXPORT_FILE,
                    payload=artifact,
                )
                await session.commit()
                should_deliver = True
        if not should_deliver:
            remove_export_artifact(self.export_dir, artifact["artifact_name"])
            return
        log.info(
            "export generated job_id=%s user_id=%s mode=%s items=%s contents=%s size_bytes=%s",
            job.id,
            job.user_id,
            job.mode,
            len(snapshot.items),
            len(snapshot.contents),
            artifact["size_bytes"],
        )

    async def fail(self, job_id: int, error: ExportError) -> None:
        """Persist a controlled failure and its Telegram notice in the same transaction."""
        from app.services.delivery import enqueue_export_delivery

        async with self.session_factory() as session:
            job = await session.get(ExportJob, job_id)
            if job is None or job.status != "RUNNING":
                return
            job.status = "FAILED"
            job.error_code = error.code[:64]
            job.error_message = str(error)[:500]
            await enqueue_export_delivery(
                session,
                user_id=job.user_id,
                export_job_id=job.id,
                delivery_type=EXPORT_FAILED,
                payload={},
            )
            await session.commit()


async def cleanup_export_artifacts(
    session_factory: async_sessionmaker,
    export_dir: str | Path,
    *,
    retention_seconds: int,
    now: datetime | None = None,
) -> int:
    """Expire old app-owned files while retaining every artifact with active delivery."""
    active_names: set[str] = set()
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(Delivery).where(
                        Delivery.type == EXPORT_FILE,
                        Delivery.status.in_(("PENDING", "SENDING")),
                    )
                )
            ).all()
        )
        for delivery in rows:
            payload = delivery.payload_json
            name = payload.get("artifact_name") if isinstance(payload, dict) else None
            if isinstance(name, str) and _EXPORT_NAME.fullmatch(name):
                active_names.add(name)

    root = ensure_export_directory(export_dir)
    cutoff = (now or datetime.now(UTC)).timestamp() - retention_seconds
    removed = 0
    try:
        paths = tuple(root.iterdir())
    except OSError:
        log.warning("export artifact cleanup could not list directory")
        return 0
    for path in paths:
        if _EXPORT_NAME.fullmatch(path.name) is None and _TEMP_NAME.fullmatch(path.name) is None:
            continue
        if path.name in active_names or path.is_symlink():
            continue
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            log.warning("export artifact cleanup could not remove an expired file")
        else:
            removed += 1
    return removed


# ❌ Удалена повторная requeue_running_export_jobs: единственная реализация выше
# остаётся startup boundary для RUNNING job recovery.
