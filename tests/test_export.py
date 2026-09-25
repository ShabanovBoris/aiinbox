import asyncio
import json
import os
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.bot.handlers import on_export
from app.domain.enums import ContentKind, ItemState, ItemType, ProcessingStatus, SourceType
from app.services.delivery import EXPORT_FILE, DeliveryWorker
from app.services.export import (
    COMPACT,
    FULL,
    ExportError,
    ExportService,
    build_export_snapshot,
    claim_oldest_export_job,
    cleanup_export_artifacts,
    enqueue_export,
    ensure_export_directory,
    requeue_running_export_jobs,
    resolve_export_artifact,
)
from app.storage.models import (
    AskJob,
    Content,
    Delivery,
    Event,
    ExportJob,
    FeedbackCallbackReceipt,
    Item,
    ItemSource,
    Reminder,
    User,
)
from app.workers.export import ExportWorker


def _telegram_message(text: str, *, user_id: int = 42, message_id: int = 1):
    """Build only the Telegram fields consumed by the thin export handler."""
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=7001),
        message_id=message_id,
        answer=AsyncMock(),
    )


async def _create_item(
    session,
    user_id: int,
    *,
    title: str,
    state=ItemState.ACTIVE,
    message_id: int = 123456789,
) -> Item:
    """Create one canonical Item row for export projection tests."""
    item = Item(
        user_id=user_id,
        telegram_message_id=message_id,
        source_index=0,
        processing_status=ProcessingStatus.READY,
        state=state,
        source_type=SourceType.TEXT,
        source_url=None,
        source_file_id="FILEID_DO_NOT_EXPORT",
        source_metadata_json={
            "forwarded": True,
            "forward_origin_type": "channel",
            "forward_source_name": "  Useful Channel  ",
            "forward_source_username": "useful_channel",
            "forward_message_id": 321,
            "original_sent_at": "2026-09-24T10:00:00Z",
            "telegram_user_id": 998877665544,
            "untrusted_marker": "ITEM_METADATA_PRIVATE_MARKER",
        },
        user_note="User note #1",
        title=title,
        summary="A compact summary.",
        category="AI",
        item_type=ItemType.READ,
        tags_json=["agents", "reading"],
        importance=0.8,
        urgency=0.4,
        goal_fit=0.9,
        long_term_value=0.7,
        interest_fit=0.8,
        estimated_action_minutes=15,
        priority_score=81,
        interest_level=3,
        priority_reason="Useful for the current goal.",
        next_action="Read the article.",
        suggested_due_at=datetime(2026, 9, 27, 12, tzinfo=UTC),
        language="en",
        confidence=0.95,
        analysis_completeness="COMPLETE",
        completed_at=datetime(2026, 9, 20, 8),
        archived_at=None,
        snoozed_until=datetime(2026, 9, 27, 12),
    )
    session.add(item)
    await session.flush()
    return item


async def _seed_export_content(session_factory):
    """Seed two owners and the canonical evidence/history used by PM-15 projections."""
    async with session_factory() as session:
        user = User(
            telegram_user_id=42,
            telegram_chat_id=998877665544,
            timezone="Europe/Moscow",
            profile_json={
                "preferred_language": "en",
                "profession": "Engineer",
                "interests": ["AI"],
                "free_text": "PROFILE_USER_OWNED_MARKER",
            },
            settings_json={
                "attention_intensity": 5,
                "operator_secret": "SETTINGS_INTERNAL_MARKER",
            },
        )
        other_user = User(telegram_user_id=1000, telegram_chat_id=776655443322)
        session.add_all([user, other_user])
        await session.flush()
        item = await _create_item(session, user.id, title="My #1 saved article")
        other_item = await _create_item(session, other_user.id, title="OTHER_OWNER_PRIVATE_MARKER")
        source = ItemSource(
            item_id=item.id,
            source_index=0,
            source_type=SourceType.WEB,
            source_url="https://example.com/article",
            content_duration_seconds=91,
            extraction_status="READY",
            source_file_id="SOURCE_FILE_ID_PRIVATE_MARKER",
            metadata_json={"failure_permanent": True, "debug": "SOURCE_INTERNAL_MARKER"},
        )
        other_source = ItemSource(
            item_id=other_item.id,
            source_index=0,
            source_type=SourceType.WEB,
            source_url="https://other.example/private",
            extraction_status="READY",
        )
        session.add_all([source, other_source])
        await session.flush()
        primary_kinds = (
            ContentKind.USER_TEXT,
            ContentKind.WEB_TEXT,
            ContentKind.DOCUMENT_TEXT,
            ContentKind.TRANSCRIPT,
            ContentKind.VISUAL_NOTES,
            ContentKind.DESCRIPTION,
        )
        for index, kind in enumerate(primary_kinds):
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source.id if index else None,
                    kind=kind,
                    text=f"PRIMARY_CONTENT_{kind.value}_MARKER",
                    metadata_json={"content_internal_marker": "CONTENT_METADATA_PRIVATE_MARKER"},
                )
            )
        for kind in (
            ContentKind.CHUNK_SUMMARY,
            ContentKind.TRANSCRIPT_CHUNK,
            ContentKind.ATTENTION_HOOK,
        ):
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source.id,
                    kind=kind,
                    text=f"EXCLUDED_CONTENT_{kind.value}_MARKER",
                )
            )
        session.add(
            Content(
                item_id=other_item.id,
                source_id=other_source.id,
                kind=ContentKind.WEB_TEXT,
                text="OTHER_OWNER_CONTENT_PRIVATE_MARKER",
            )
        )
        reminder = Reminder(
            user_id=user.id,
            item_id=item.id,
            type="PROACTIVE_ATTENTION",
            scheduled_at=datetime(2026, 9, 24, 10),
            status="SENT",
            payload_json={
                "category": "AI",
                "policy_level": 3,
                "hook_content_id": 999,
                "claim_generation": 777777777,
                "payload_private_marker": "REMINDER_INTERNAL_MARKER",
            },
            claimed_at=datetime(2026, 9, 24, 9),
            claim_generation=7,
            sent_at=datetime(2026, 9, 24, 10),
        )
        other_reminder = Reminder(
            user_id=other_user.id,
            item_id=other_item.id,
            type="SNOOZE_RESURFACE",
            scheduled_at=datetime(2026, 9, 24, 11),
            status="SENT",
        )
        session.add_all([reminder, other_reminder])
        await session.flush()
        session.add_all(
            [
                Event(
                    user_id=user.id,
                    item_id=item.id,
                    event_type="CATEGORY_CORRECTED",
                    payload_json={
                        "from": "Old",
                        "to": "AI",
                        "source": "telegram",
                        "internal_secret_debug": "EVENT_INTERNAL_MARKER",
                    },
                    created_at=datetime(2026, 9, 23, 10),
                ),
                Event(
                    user_id=user.id,
                    item_id=item.id,
                    reminder_id=reminder.id,
                    event_type="REMINDER_SENT",
                    payload_json={
                        "reminder_type": "PROACTIVE_ATTENTION",
                        "policy_level": 3,
                        "category": "AI",
                        "item_type": "READ",
                        "motivation_kind": "QUICK_WINS",
                        "template_id": "quick_win_1",
                        "hook_content_id": 999,
                        "source_id": source.id,
                        "internal_secret_debug": "REMINDER_EVENT_PRIVATE_MARKER",
                    },
                    created_at=datetime(2026, 9, 24, 10),
                ),
                Event(
                    user_id=other_user.id,
                    item_id=other_item.id,
                    event_type="CREATED",
                    created_at=datetime(2026, 9, 24, 11),
                ),
            ]
        )
        ask_job = AskJob(
            user_id=user.id,
            telegram_message_id=777,
            question="ASK_QUESTION_PRIVATE_MARKER",
            status="DONE",
        )
        session.add(ask_job)
        await session.flush()
        session.add_all(
            [
                Delivery(
                    user_id=user.id,
                    ask_job_id=ask_job.id,
                    type="ASK_RESULT",
                    status="SENT",
                    payload_json={"answer": "ASK_ANSWER_PRIVATE_MARKER"},
                ),
                FeedbackCallbackReceipt(
                    user_id=user.id,
                    idempotency_key="CALLBACK_RECEIPT_PRIVATE_MARKER",
                ),
            ]
        )
        await session.commit()
        return user.id, item.id, other_item.id, source.id, reminder.id


async def _generate_export(session_factory, export_dir: Path, user_id: int, *, mode: str):
    """Run one durable generation through the same claim and finalization path as production."""
    job = await enqueue_export(
        session_factory,
        telegram_user_id=42,
        telegram_message_id=900 if mode == COMPACT else 901,
        chat_id=7001,
        mode=mode,
        default_timezone="UTC",
    )
    claimed = await claim_oldest_export_job(session_factory)
    assert claimed is not None and claimed.id == job.id
    service = ExportService(session_factory, export_dir, max_content_chars=100_000)
    await service.process(claimed)
    async with session_factory() as session:
        stored_job = await session.get(ExportJob, job.id)
        delivery = await session.scalar(
            select(Delivery).where(
                Delivery.export_job_id == job.id,
                Delivery.type == EXPORT_FILE,
            )
        )
        assert stored_job.status == "DONE"
        assert delivery is not None and delivery.status == "PENDING"
        return stored_job, delivery


async def _archive_bytes(path: Path) -> bytes:
    """Return every fixed archive member as bytes for absence/presence assertions."""
    with zipfile.ZipFile(path) as archive:
        return b"\n".join(archive.read(name) for name in archive.namelist())


@pytest.mark.asyncio
async def test_export_command_defaults_to_compact_and_deduplicates_telegram_message(
    settings, session_factory
):
    message = _telegram_message("/export", message_id=12)
    await on_export(message, settings, session_factory)
    await on_export(_telegram_message("/export", message_id=12), settings, session_factory)

    async with session_factory() as session:
        jobs = list((await session.scalars(select(ExportJob))).all())
        assert len(jobs) == 1
        assert jobs[0].mode == COMPACT
        assert jobs[0].status == "PENDING"
    assert message.answer.await_args.args == ("Готовлю компактный экспорт…",)


@pytest.mark.asyncio
async def test_export_command_accepts_case_insensitive_full_and_rejects_invalid_or_unauthorized(
    settings, session_factory
):
    full = _telegram_message("/export FULL", message_id=13)
    await on_export(full, settings, session_factory)
    invalid = _telegram_message("/export pdf", message_id=14)
    await on_export(invalid, settings, session_factory)
    extra_argument = _telegram_message("/export compact now", message_id=15)
    await on_export(extra_argument, settings, session_factory)
    unauthorized = _telegram_message("/export", user_id=666, message_id=16)
    await on_export(unauthorized, settings, session_factory)

    async with session_factory() as session:
        jobs = list((await session.scalars(select(ExportJob))).all())
        assert [(job.telegram_message_id, job.mode) for job in jobs] == [(13, FULL)]
    assert full.answer.await_args.args == ("Готовлю полный экспорт…",)
    assert invalid.answer.await_args.args == ("Использование: /export [compact|full]",)
    assert extra_argument.answer.await_args.args == ("Использование: /export [compact|full]",)
    unauthorized.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_enqueue_rejects_invalid_mode_without_creating_a_job(
    settings, session_factory
):
    with pytest.raises(ValueError, match="compact or full"):
        await enqueue_export(
            session_factory,
            telegram_user_id=42,
            telegram_message_id=17,
            chat_id=7001,
            mode="PDF",
        )
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ExportJob)) == 0


@pytest.mark.asyncio
async def test_full_snapshot_enforces_content_bound_before_loading_rows(session_factory):
    user_id, _, _, _, _ = await _seed_export_content(session_factory)
    with pytest.raises(ExportError, match="safety limit") as error:
        await build_export_snapshot(
            session_factory,
            user_id=user_id,
            mode=FULL,
            max_content_chars=10,
        )
    assert error.value.code == "EXPORT_CONTENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_compact_and_full_archives_obey_allowlists_and_user_ownership(
    tmp_path, session_factory
):
    user_id, item_id, other_item_id, source_id, reminder_id = await _seed_export_content(
        session_factory
    )
    compact_dir = ensure_export_directory(tmp_path / "compact")
    full_dir = ensure_export_directory(tmp_path / "full")
    _, compact_delivery = await _generate_export(
        session_factory, compact_dir, user_id, mode=COMPACT
    )
    _, full_delivery = await _generate_export(session_factory, full_dir, user_id, mode=FULL)
    compact_path = resolve_export_artifact(
        compact_dir, compact_delivery.payload_json["artifact_name"]
    )
    full_path = resolve_export_artifact(full_dir, full_delivery.payload_json["artifact_name"])

    with zipfile.ZipFile(compact_path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "README.md",
            "profile.json",
            "settings.json",
            "items.jsonl",
            "sources.jsonl",
            "events.jsonl",
            "reminders.jsonl",
            "items.md",
        }
        manifest = json.loads(archive.read("manifest.json"))
        compact_items = [json.loads(line) for line in archive.read("items.jsonl").splitlines()]
        compact_sources = [json.loads(line) for line in archive.read("sources.jsonl").splitlines()]
        compact_events = [json.loads(line) for line in archive.read("events.jsonl").splitlines()]
        compact_reminders = [
            json.loads(line) for line in archive.read("reminders.jsonl").splitlines()
        ]
        profile = json.loads(archive.read("profile.json"))
        settings_json = json.loads(archive.read("settings.json"))
        assert manifest["format"] == "aiinbox-export" and manifest["version"] == 1
        assert manifest["mode"] == "compact" and manifest["counts"]["contents"] == 0
        assert manifest["counts"]["items"] == 1
        assert profile["free_text"] == "PROFILE_USER_OWNED_MARKER"
        assert settings_json == {
            "timezone": "Europe/Moscow",
            "daily_digest_enabled": True,
            "daily_digest_time": "09:00",
            "quiet_hours_start": "22:30",
            "quiet_hours_end": "08:00",
            "attention_enabled": True,
            "attention_intensity": 5,
            "generic_motivation_enabled": True,
        }
        assert compact_items[0]["id"] == item_id
        assert compact_items[0]["state"] == "ACTIVE"
        assert compact_items[0]["source_provenance"] == {
            "forwarded": True,
            "forward_origin_type": "channel",
            "forward_source_name": "Useful Channel",
            "forward_source_username": "useful_channel",
            "forward_message_id": 321,
            "original_sent_at": "2026-09-24T10:00:00Z",
        }
        assert compact_sources == [
            {
                "id": source_id,
                "item_id": item_id,
                "source_index": 0,
                "source_type": "WEB",
                "source_url": "https://example.com/article",
                "content_duration_seconds": 91,
                "extraction_status": "READY",
                "created_at": compact_sources[0]["created_at"],
                "updated_at": compact_sources[0]["updated_at"],
            }
        ]
        assert compact_events[0]["payload"] == {
            "from": "Old",
            "to": "AI",
            "source": "telegram",
        }
        assert compact_events[1]["payload"]["source_id"] == source_id
        assert compact_events[1]["reminder_id"] == reminder_id
        assert compact_reminders[0]["item_id"] == item_id
        assert "payload_json" not in compact_reminders[0]
        assert "contents.jsonl" not in archive.namelist()

    compact_bytes = await _archive_bytes(compact_path)
    for private_marker in (
        "PRIMARY_CONTENT_",
        "EXCLUDED_CONTENT_",
        "OTHER_OWNER_PRIVATE_MARKER",
        "OTHER_OWNER_CONTENT_PRIVATE_MARKER",
        "FILEID_DO_NOT_EXPORT",
        "SOURCE_FILE_ID_PRIVATE_MARKER",
        "ITEM_METADATA_PRIVATE_MARKER",
        "SOURCE_INTERNAL_MARKER",
        "CONTENT_METADATA_PRIVATE_MARKER",
        "EVENT_INTERNAL_MARKER",
        "REMINDER_INTERNAL_MARKER",
        "REMINDER_EVENT_PRIVATE_MARKER",
        "SETTINGS_INTERNAL_MARKER",
        "ASK_QUESTION_PRIVATE_MARKER",
        "ASK_ANSWER_PRIVATE_MARKER",
        "CALLBACK_RECEIPT_PRIVATE_MARKER",
    ):
        assert private_marker.encode() not in compact_bytes
    assert b"998877665544" not in compact_bytes
    assert b"123456789" not in compact_bytes

    with zipfile.ZipFile(full_path) as archive:
        assert "contents.jsonl" in archive.namelist()
        contents = [json.loads(line) for line in archive.read("contents.jsonl").splitlines()]
        assert [record["kind"] for record in contents] == [
            "USER_TEXT",
            "WEB_TEXT",
            "DOCUMENT_TEXT",
            "TRANSCRIPT",
            "VISUAL_NOTES",
            "DESCRIPTION",
        ]
        assert {record["item_id"] for record in contents} == {item_id}
        assert all(record["source_id"] in {None, source_id} for record in contents)
        assert other_item_id not in {record["item_id"] for record in contents}
        readme = archive.read("README.md").decode()
        markdown = archive.read("items.md").decode()
        assert "contents.jsonl" in readme
        assert "### Моя заметка" in markdown
    full_bytes = await _archive_bytes(full_path)
    for excluded in (
        "EXCLUDED_CONTENT_CHUNK_SUMMARY_MARKER",
        "EXCLUDED_CONTENT_TRANSCRIPT_CHUNK_MARKER",
        "EXCLUDED_CONTENT_ATTENTION_HOOK_MARKER",
        "OTHER_OWNER_PRIVATE_MARKER",
        "OTHER_OWNER_CONTENT_PRIVATE_MARKER",
        "ASK_ANSWER_PRIVATE_MARKER",
    ):
        assert excluded.encode() not in full_bytes

    async with session_factory() as session:
        current_item = await session.get(Item, item_id)
        assert current_item.state is ItemState.ACTIVE
        assert current_item.processing_status is ProcessingStatus.READY
        assert await session.scalar(select(func.count()).select_from(Content)) == 10
        assert await session.scalar(select(func.count()).select_from(Event)) == 3
        assert await session.scalar(select(func.count()).select_from(Reminder)) == 2
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 3
        assert await session.scalar(select(func.count()).select_from(ExportJob)) == 2


@pytest.mark.asyncio
async def test_export_worker_claim_is_atomic_and_running_jobs_recover(session_factory):
    await enqueue_export(
        session_factory,
        telegram_user_id=42,
        telegram_message_id=31,
        chat_id=7001,
        mode=COMPACT,
    )
    first, second = await asyncio.gather(
        claim_oldest_export_job(session_factory),
        claim_oldest_export_job(session_factory),
    )
    assert sum(job is not None for job in (first, second)) == 1
    assert await requeue_running_export_jobs(session_factory) == 1
    again = await claim_oldest_export_job(session_factory)
    assert again is not None and again.status == "RUNNING"


@pytest.mark.asyncio
async def test_compact_snapshot_includes_every_lifecycle_and_processing_status(
    session_factory,
):
    """Ownership export covers the whole canonical Item table, not an inbox filter."""
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=7001)
        session.add(user)
        await session.flush()
        rows = (
            (ItemState.ACTIVE, ProcessingStatus.READY),
            (ItemState.SNOOZED, ProcessingStatus.QUEUED),
            (ItemState.DONE, ProcessingStatus.FAILED),
            (ItemState.ARCHIVED, ProcessingStatus.PROCESSING),
        )
        for index, (state, status) in enumerate(rows, start=1):
            session.add(
                Item(
                    user_id=user.id,
                    telegram_message_id=2000 + index,
                    source_index=0,
                    processing_status=status,
                    state=state,
                    source_type=SourceType.TEXT,
                    user_note="",
                )
            )
        user_id = user.id
        await session.commit()

    snapshot = await build_export_snapshot(
        session_factory,
        user_id=user_id,
        mode=COMPACT,
        max_content_chars=100,
    )
    assert {(item.state, item.processing_status) for item in snapshot.items} == {
        (state.value, status.value) for state, status in rows
    }


@pytest.mark.asyncio
async def test_full_content_guard_creates_controlled_durable_failure_notice(
    tmp_path, session_factory
):
    """A full-export safety bound fails the job visibly and never creates a partial ZIP."""
    user_id, _, _, _, _ = await _seed_export_content(session_factory)
    export_dir = ensure_export_directory(tmp_path / "exports")
    job = await enqueue_export(
        session_factory,
        telegram_user_id=42,
        telegram_message_id=51,
        chat_id=7001,
        mode=FULL,
    )
    worker = ExportWorker(
        session_factory,
        str(export_dir),
        max_content_chars=1,
        retention_seconds=3600,
    )
    assert await worker.process_one()

    async with session_factory() as session:
        failed_job = await session.get(ExportJob, job.id)
        notice = await session.scalar(
            select(Delivery).where(
                Delivery.export_job_id == job.id,
                Delivery.type == "EXPORT_FAILED",
            )
        )
        assert failed_job.status == "FAILED"
        assert failed_job.error_code == "EXPORT_CONTENT_TOO_LARGE"
        assert notice is not None and notice.status == "PENDING"
    assert list(export_dir.iterdir()) == []

    bot = SimpleNamespace(send_message=AsyncMock())
    delivery_worker = DeliveryWorker(
        session_factory,
        bot,
        export_dir=export_dir,
        retry_backoff_seconds=0,
    )
    assert await delivery_worker.process_one()
    assert bot.send_message.await_args.args == (
        998877665544,
        "Полный экспорт получился слишком большим для отправки в Telegram. "
        "Попробуй /export compact.",
    )
    assert user_id > 0


@pytest.mark.asyncio
async def test_oversized_zip_fails_without_downgrading_to_compact(
    tmp_path, session_factory, monkeypatch
):
    """Telegram size rejection leaves an explicit failure and no mislabeled archive."""
    user = User(telegram_user_id=42, telegram_chat_id=7001)
    async with session_factory() as session:
        session.add(user)
        await session.commit()
    export_dir = ensure_export_directory(tmp_path / "exports")
    job = await enqueue_export(
        session_factory,
        telegram_user_id=42,
        telegram_message_id=52,
        chat_id=7001,
        mode=FULL,
    )
    monkeypatch.setattr("app.services.delivery.TELEGRAM_MAX_UPLOAD_BYTES", 1)
    worker = ExportWorker(
        session_factory,
        str(export_dir),
        max_content_chars=100,
        retention_seconds=3600,
    )
    await worker.process_one()
    async with session_factory() as session:
        failed_job = await session.get(ExportJob, job.id)
        assert failed_job.status == "FAILED"
        assert failed_job.error_code == "EXPORT_TOO_LARGE"
        assert (
            await session.scalar(select(Delivery.type).where(Delivery.export_job_id == job.id))
            == "EXPORT_FAILED"
        )
    assert list(export_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_delivery_reuses_archive_on_retry_and_deletes_only_after_sent(
    tmp_path, session_factory
):
    user_id, _, _, _, _ = await _seed_export_content(session_factory)
    export_dir = ensure_export_directory(tmp_path / "exports")
    _, delivery = await _generate_export(session_factory, export_dir, user_id, mode=FULL)
    artifact_name = delivery.payload_json["artifact_name"]
    artifact = resolve_export_artifact(export_dir, artifact_name)
    bot = SimpleNamespace(send_document=AsyncMock(side_effect=RuntimeError("temporary transport")))
    worker = DeliveryWorker(
        session_factory,
        bot,
        export_dir=export_dir,
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    assert await worker.process_one()
    async with session_factory() as session:
        stored = await session.get(Delivery, delivery.id)
        assert stored.status == "PENDING"
        assert stored.payload_json["artifact_name"] == artifact_name
        assert (await session.get(ExportJob, delivery.export_job_id)).status == "DONE"
    assert artifact.is_file()

    bot.send_document.side_effect = None
    assert await worker.process_one()
    async with session_factory() as session:
        stored = await session.get(Delivery, delivery.id)
        assert stored.status == "SENT"
        assert stored.payload_json["artifact_name"] == artifact_name
    assert not artifact.exists()
    assert bot.send_document.await_count == 2
    assert bot.send_document.await_args.kwargs["document"].path == artifact


@pytest.mark.asyncio
async def test_delivery_rejects_traversal_and_symlink_artifacts(tmp_path, session_factory):
    user_id, _, _, _, _ = await _seed_export_content(session_factory)
    export_dir = ensure_export_directory(tmp_path / "exports")
    job = ExportJob(
        user_id=user_id,
        telegram_message_id=44,
        mode=COMPACT,
        status="DONE",
    )
    async with session_factory() as session:
        session.add(job)
        await session.flush()
        delivery = Delivery(
            user_id=user_id,
            export_job_id=job.id,
            type=EXPORT_FILE,
            status="PENDING",
            payload_json={"artifact_name": "../outside.zip", "mode": COMPACT, "size_bytes": 1},
        )
        session.add(delivery)
        await session.commit()
        delivery_id = delivery.id
        job_id = job.id

    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"private")
    bot = SimpleNamespace(send_document=AsyncMock())
    worker = DeliveryWorker(
        session_factory,
        bot,
        export_dir=export_dir,
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    await worker.process_one()
    async with session_factory() as session:
        assert (await session.get(Delivery, delivery_id)).status == "FAILED"
        assert (await session.get(ExportJob, job_id)).status == "DONE"
    bot.send_document.assert_not_awaited()
    assert outside.read_bytes() == b"private"

    target = export_dir / "source.zip"
    target.write_bytes(b"private")
    symlink_name = f"aiinbox-export-{job_id}-{uuid4().hex}.zip"
    (export_dir / symlink_name).symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        resolve_export_artifact(export_dir, symlink_name)


@pytest.mark.asyncio
async def test_cleanup_preserves_active_artifact_and_unrelated_files(tmp_path, session_factory):
    export_dir = ensure_export_directory(tmp_path / "exports")
    user = User(telegram_user_id=42, telegram_chat_id=7001)
    async with session_factory() as session:
        session.add(user)
        await session.flush()
        job = ExportJob(
            user_id=user.id,
            telegram_message_id=55,
            mode=COMPACT,
            status="DONE",
        )
        session.add(job)
        await session.flush()
        active_name = f"aiinbox-export-{job.id}-{uuid4().hex}.zip"
        session.add(
            Delivery(
                user_id=user.id,
                export_job_id=job.id,
                type=EXPORT_FILE,
                status="PENDING",
                payload_json={"artifact_name": active_name},
            )
        )
        await session.commit()

    active_file = export_dir / active_name
    orphan_file = export_dir / f"aiinbox-export-999-{uuid4().hex}.zip"
    temporary_file = export_dir / "aiinbox-export-999-old-run.tmp"
    unrelated_file = export_dir / "manual.txt"
    for path in (active_file, orphan_file, temporary_file, unrelated_file):
        path.write_text("artifact", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    for path in (active_file, orphan_file, temporary_file, unrelated_file):
        os.utime(path, (old_time, old_time))

    removed = await cleanup_export_artifacts(
        session_factory,
        export_dir,
        retention_seconds=3600,
    )
    assert removed == 2
    assert active_file.exists()
    assert not orphan_file.exists()
    assert not temporary_file.exists()
    assert unrelated_file.exists()
    assert stat.S_IMODE(export_dir.stat().st_mode) == 0o700
