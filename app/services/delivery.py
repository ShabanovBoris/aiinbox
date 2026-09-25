"""Durable outbox for notifications and user-requested Telegram media sends.

The business transaction only records an intent. A separate worker owns the
network side effect, so restart recovery never requires rolling READY/FAILED
Items or completed profile jobs back to an earlier business state.
"""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, ReplyParameters
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from app.bot.formatting import format_ask_answer
from app.bot.keyboards import ask_sources_keyboard
from app.bot.notify import send_item_failure, send_item_result
from app.domain.enums import ProcessingStatus, SourceType
from app.domain.models import AskDeliveryPayload, AskReference
from app.errors import AppError, MediaTooLargeError
from app.extractors.subprocess_runner import cleanup_temporary_directory
from app.services.ask_inbox import safe_http_url, unique_item_source_url
from app.storage.models import AskJob, Delivery, ExportJob, Item, ItemSource, User

log = logging.getLogger(__name__)

ITEM_READY = "ITEM_READY"
ITEM_FAILED = "ITEM_FAILED"
PROFILE_UPDATED = "PROFILE_UPDATED"
ASK_RESULT = "ASK_RESULT"
ASK_FAILED = "ASK_FAILED"
EXPORT_FILE = "EXPORT_FILE"
EXPORT_FAILED = "EXPORT_FAILED"
ITEM_VIDEO_PREFIX = "ITEM_VIDEO:"
TELEGRAM_MAX_UPLOAD_BYTES = 50_000_000


def _is_telegram_upload_size_error(exc: Exception) -> bool:
    """Limit audio fallback to explicit Telegram size rejections, never generic send errors."""
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "request entity too large",
            "file is too big",
            "file is too large",
            "payload too large",
        )
    )


async def enqueue_item_delivery(
    session,
    item: Item,
    delivery_type: str,
    *,
    payload: dict | None = None,
    reopen: bool = False,
) -> Delivery:
    """Persist an Item delivery intent inside the caller's business transaction.

    A caller may explicitly reopen a terminal key when a new processing pass
    produces a new result, such as FAILED retry or PARTIAL source recovery.
    """
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.item_id == item.id,
            Delivery.type == delivery_type,
        )
    )
    if existing is not None:
        if reopen and existing.status in {"SENT", "FAILED", "CANCELLED"}:
            existing.status = "PENDING"
            existing.attempts = 0
            existing.last_error = None
            existing.sent_at = None
        if payload is not None:
            existing.payload_json = payload
        return existing

    delivery = Delivery(
        user_id=item.user_id,
        item_id=item.id,
        type=delivery_type,
        status="PENDING",
        payload_json=payload,
    )
    session.add(delivery)
    return delivery


async def enqueue_item_video_delivery(
    session_factory,
    telegram_user_id: int,
    item_id: int,
    source_id: int,
) -> str | None:
    """Persist one user-scoped media-send request without downloading in Telegram's handler.

    The outbox key includes the child source id so a composite Item can expose
    independent YouTube/Reel actions while repeated taps converge on one request.
    """
    async with session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        user_id = await session.scalar(
            select(User.id).where(User.telegram_user_id == telegram_user_id)
        )
        if user_id is None:
            await session.rollback()
            return None
        result = await enqueue_item_video_delivery_in_session(session, user_id, item_id, source_id)
        if result is None:
            await session.rollback()
            return None
        if result == "IN_PROGRESS":
            await session.rollback()
            return result
        await session.commit()
        return result


async def enqueue_item_video_delivery_in_session(
    session, user_id: int, item_id: int, source_id: int
) -> str | None:
    """Reuse the existing media eligibility/outbox rules inside a caller-owned transaction.

    Reminder source callbacks compose this intent with REMINDER_OPENED atomically;
    the actual download and Telegram send remain owned by DeliveryWorker.
    """
    item = await session.scalar(
        select(Item).where(
            Item.id == item_id,
            Item.user_id == user_id,
            Item.processing_status == ProcessingStatus.READY,
        )
    )
    source = await session.get(ItemSource, source_id)
    if (
        item is None
        or source is None
        or source.item_id != item_id
        or source.extraction_status != "READY"
        or source.source_type not in {SourceType.YOUTUBE, SourceType.INSTAGRAM}
        or not source.source_url
    ):
        return None

    delivery_type = f"{ITEM_VIDEO_PREFIX}{source.id}"
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.item_id == item.id,
            Delivery.type == delivery_type,
        )
    )
    if existing is not None and existing.status in {"PENDING", "SENDING"}:
        return "IN_PROGRESS"

    payload = dict(existing.payload_json or {}) if existing is not None else {}
    if payload.get("telegram_media_kind") != "video":
        # ❌ Удален кэш generic document file_id: Telegram не сообщает, что это видео,
        # поэтому старый audio/document id мог повторяться по кнопке отправки видео.
        payload.pop("telegram_file_id", None)
        payload.pop("telegram_media_kind", None)
    payload["source_id"] = source.id
    await enqueue_item_delivery(
        session,
        item,
        delivery_type,
        payload=payload,
        reopen=existing is not None,
    )
    return "QUEUED"


async def enqueue_profile_delivery(
    session,
    *,
    user_id: int,
    profile_update_job_id: int,
    changed: list[str],
) -> Delivery:
    """Record profile completion together with its stable notification payload."""
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.profile_update_job_id == profile_update_job_id,
            Delivery.type == PROFILE_UPDATED,
        )
    )
    if existing is not None:
        return existing
    delivery = Delivery(
        user_id=user_id,
        profile_update_job_id=profile_update_job_id,
        type=PROFILE_UPDATED,
        status="PENDING",
        payload_json={"changed": changed},
    )
    session.add(delivery)
    return delivery


async def enqueue_ask_delivery(
    session,
    *,
    user_id: int,
    ask_job_id: int,
    delivery_type: str,
    payload: dict,
) -> Delivery:
    """Keep one synthesis outcome per durable AskJob and delivery type."""
    if delivery_type not in {ASK_RESULT, ASK_FAILED}:
        raise ValueError(f"unsupported Ask delivery type={delivery_type}")
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.ask_job_id == ask_job_id,
            Delivery.type == delivery_type,
        )
    )
    if existing is not None:
        return existing
    delivery = Delivery(
        user_id=user_id,
        ask_job_id=ask_job_id,
        type=delivery_type,
        status="PENDING",
        payload_json=payload,
    )
    session.add(delivery)
    return delivery


async def enqueue_export_delivery(
    session,
    *,
    user_id: int,
    export_job_id: int,
    delivery_type: str,
    payload: dict,
) -> Delivery:
    """Record exactly one export outcome beside its durable ExportJob transition."""
    if delivery_type not in {EXPORT_FILE, EXPORT_FAILED}:
        raise ValueError(f"unsupported export delivery type={delivery_type}")
    existing = await session.scalar(
        select(Delivery).where(
            Delivery.export_job_id == export_job_id,
            Delivery.type == delivery_type,
        )
    )
    if existing is not None:
        return existing
    delivery = Delivery(
        user_id=user_id,
        export_job_id=export_job_id,
        type=delivery_type,
        status="PENDING",
        payload_json=payload,
    )
    session.add(delivery)
    return delivery


async def _load_ask_references(session, user_id: int, payload: AskDeliveryPayload):
    """Resolve transient citation IDs from their owning user-scoped database rows."""
    citations = payload.references
    item_ids = {citation.item_id for citation in citations}
    if not item_ids:
        return ()
    items = list(
        (
            await session.scalars(
                select(Item).where(Item.user_id == user_id, Item.id.in_(item_ids))
            )
        ).all()
    )
    item_by_id = {item.id: item for item in items}
    if item_by_id.keys() != item_ids:
        raise RuntimeError("Ask delivery references an unavailable Item")
    sources = list(
        (
            await session.scalars(
                select(ItemSource)
                .where(ItemSource.item_id.in_(item_ids))
                .order_by(ItemSource.item_id, ItemSource.source_index, ItemSource.id)
            )
        ).all()
    )
    sources_by_item: dict[int, list[ItemSource]] = {item_id: [] for item_id in item_ids}
    source_by_id = {}
    for source in sources:
        sources_by_item[source.item_id].append(source)
        source_by_id[source.id] = source

    references = []
    for citation in citations:
        item = item_by_id[citation.item_id]
        if citation.source_id is None:
            source_type = None
            source_url = unique_item_source_url(item, sources_by_item[item.id])
        else:
            source = source_by_id.get(citation.source_id)
            if source is None or source.item_id != item.id:
                raise RuntimeError("Ask delivery references an unavailable source")
            source_type = source.source_type.value
            source_url = safe_http_url(source.source_url)
        references.append(
            AskReference(
                item_id=item.id,
                source_id=citation.source_id,
                title=(item.title or "(untitled)")[:120],
                source_type=source_type,
                source_url=source_url,
                # `_send` already requires the owning User's current chat before
                # resolving references, so this Item identity can be copied safely.
                original_available=item.telegram_message_id is not None,
            )
        )
    return tuple(references)


async def requeue_sending_deliveries(session_factory) -> int:
    """Recover the side-effect boundary after process death.

    SENDING means the previous process claimed the row but never durably marked
    completion. Requeueing prefers possible duplicate delivery over silent loss.
    """
    async with session_factory() as session:
        result = await session.execute(
            update(Delivery).where(Delivery.status == "SENDING").values(status="PENDING")
        )
        await session.commit()
        if result.rowcount:
            log.warning("requeued interrupted deliveries count=%s", result.rowcount)
        return result.rowcount


class DeliveryWorker:
    """Owns Telegram side effects for the durable immediate-delivery outbox.

    Claim and attempt count are durable. Transient delivery errors return the
    row to PENDING; permanent media failures stop immediately. Database failures
    escape to the process supervisor because they compromise queue correctness.
    """

    def __init__(
        self,
        session_factory,
        bot,
        *,
        youtube_extractor=None,
        instagram_extractor=None,
        export_dir: str | Path = "./exports",
        poll_seconds: float = 1.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ):
        self.session_factory = session_factory
        self.bot = bot
        self.youtube_extractor = youtube_extractor
        self.instagram_extractor = instagram_extractor
        self.export_dir = Path(export_dir)
        self.poll_seconds = poll_seconds
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = await self.process_one()
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    async def claim_next(self) -> int | None:
        async with self.session_factory() as session:
            result = await session.execute(
                update(Delivery)
                .where(
                    Delivery.id
                    == select(Delivery.id)
                    .where(Delivery.status == "PENDING")
                    .order_by(Delivery.created_at, Delivery.id)
                    .limit(1)
                    .scalar_subquery(),
                    Delivery.status == "PENDING",
                )
                .values(
                    status="SENDING",
                    attempts=Delivery.attempts + 1,
                    updated_at=func.now(),
                )
                .returning(Delivery.id)
            )
            delivery_id = result.scalar_one_or_none()
            await session.commit()
            return delivery_id

    async def process_one(self) -> bool:
        delivery_id = await self.claim_next()
        if delivery_id is None:
            return False
        try:
            payload_updates = await self._send(delivery_id)
            await self._mark_sent(delivery_id, payload_updates)
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            log.exception("delivery worker database failure delivery_id=%s", delivery_id)
            raise
        except Exception as exc:
            retry = await self._record_failure(delivery_id, exc)
            log.warning(
                "telegram delivery failed delivery_id=%s retry=%s error=%s",
                delivery_id,
                retry,
                exc,
            )
            if retry and self.retry_backoff_seconds:
                await asyncio.sleep(self.retry_backoff_seconds)
        return True

    async def _send(self, delivery_id: int) -> dict | None:
        ask_text = None
        ask_keyboard = None
        export_job = None
        async with self.session_factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                raise RuntimeError(f"delivery {delivery_id} disappeared")
            user = await session.get(User, delivery.user_id)
            if user is None or user.telegram_chat_id is None:
                raise RuntimeError(f"delivery {delivery_id} has no Telegram chat")
            chat_id = user.telegram_chat_id
            delivery_type = delivery.type
            payload = dict(delivery.payload_json) if isinstance(delivery.payload_json, dict) else {}
            item = await session.get(Item, delivery.item_id) if delivery.item_id else None
            ask_job = (
                await session.get(AskJob, delivery.ask_job_id) if delivery.ask_job_id else None
            )
            if delivery_type in {EXPORT_FILE, EXPORT_FAILED}:
                export_job = (
                    await session.get(ExportJob, delivery.export_job_id)
                    if delivery.export_job_id
                    else None
                )
                expected_status = "DONE" if delivery_type == EXPORT_FILE else "FAILED"
                if (
                    export_job is None
                    or export_job.user_id != delivery.user_id
                    or export_job.status != expected_status
                ):
                    raise RuntimeError(f"delivery {delivery_id} has no matching ExportJob")
            video_source = None
            if delivery_type.startswith(ITEM_VIDEO_PREFIX):
                source_id = payload.get("source_id")
                if isinstance(source_id, bool) or not isinstance(source_id, int):
                    raise RuntimeError(f"delivery {delivery_id} has invalid video source id")
                if delivery_type != f"{ITEM_VIDEO_PREFIX}{source_id}":
                    raise RuntimeError(f"delivery {delivery_id} video source key does not match")
                video_source = await session.get(ItemSource, source_id)

            if delivery_type in {ASK_RESULT, ASK_FAILED}:
                if ask_job is None or ask_job.user_id != delivery.user_id:
                    raise RuntimeError(f"delivery {delivery_id} has no matching AskJob")
                if delivery_type == ASK_RESULT:
                    try:
                        ask_payload = AskDeliveryPayload.model_validate(payload)
                    except Exception:
                        raise RuntimeError("Ask delivery payload is invalid") from None
                    references = await _load_ask_references(session, delivery.user_id, ask_payload)
                    ask_text = format_ask_answer(ask_payload.answer, references)
                    ask_keyboard = ask_sources_keyboard(references)

        if delivery_type.startswith(ITEM_VIDEO_PREFIX):
            if item is None or video_source is None:
                raise RuntimeError(f"delivery {delivery_id} video source disappeared")
            return await self._send_item_video(chat_id, item, video_source, payload)

        if delivery_type == ITEM_READY and item is not None:
            await send_item_result(self.bot, self.session_factory, item)
            return None
        if delivery_type == ITEM_FAILED and item is not None:
            if item.processing_status is not ProcessingStatus.FAILED:
                log.info(
                    "stale failure delivery suppressed delivery_id=%s item_id=%s status=%s",
                    delivery_id,
                    item.id,
                    item.processing_status.value,
                )
                return
            await send_item_failure(self.bot, self.session_factory, item)
            return None
        if delivery_type == PROFILE_UPDATED:
            changed = payload.get("changed") or []
            await self.bot.send_message(chat_id, "Профиль обновлён: " + ", ".join(changed))
            return None
        if delivery_type == ASK_RESULT:
            try:
                await self.bot.send_message(
                    chat_id,
                    ask_text,
                    reply_markup=ask_keyboard,
                )
            except Exception as exc:
                # Telegram transport errors can include request text; keep the
                # answer only in the retryable outbox payload, never in logs.
                raise RuntimeError(f"Ask result delivery failed: {type(exc).__name__}") from None
            # Keep the durable retry payload until Telegram accepts the message,
            # then drop generated prose in the same transaction as SENT.
            return {"answer": None}
        if delivery_type == ASK_FAILED:
            await self.bot.send_message(
                chat_id,
                "Не удалось подготовить ответ по сохранённым материалам. Попробуй ещё раз.",
            )
            return None
        if delivery_type == EXPORT_FILE:
            if export_job is None:
                raise RuntimeError(f"delivery {delivery_id} has no matching ExportJob")
            from app.services.export import resolve_export_artifact

            artifact_name = payload.get("artifact_name")
            mode = payload.get("mode")
            size_bytes = payload.get("size_bytes")
            if (
                mode != export_job.mode
                or mode not in {"COMPACT", "FULL"}
                or type(size_bytes) is not int
                or size_bytes <= 0
                or size_bytes > TELEGRAM_MAX_UPLOAD_BYTES
            ):
                raise RuntimeError(f"delivery {delivery_id} has invalid export metadata")
            try:
                artifact_path = resolve_export_artifact(self.export_dir, artifact_name)
            except ValueError:
                raise RuntimeError(
                    f"delivery {delivery_id} has an unsafe export artifact"
                ) from None
            if artifact_path.stat().st_size != size_bytes:
                raise RuntimeError(f"delivery {delivery_id} export artifact size changed")
            await self.bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(artifact_path),
                caption=f"Экспорт AIInbox — {mode.casefold()}",
            )
            return None
        if delivery_type == EXPORT_FAILED:
            if export_job is None:
                raise RuntimeError(f"delivery {delivery_id} has no matching ExportJob")
            if export_job.error_code in {"EXPORT_TOO_LARGE", "EXPORT_CONTENT_TOO_LARGE"}:
                message = (
                    "Полный экспорт получился слишком большим для отправки в Telegram. "
                    "Попробуй /export compact."
                )
            else:
                message = "Не удалось подготовить экспорт. Попробуй ещё раз."
            await self.bot.send_message(chat_id, message)
            return None
        raise RuntimeError(f"unsupported delivery type={delivery_type}")

    async def _send_item_video(
        self, chat_id: int, item: Item, source: ItemSource, payload: dict
    ) -> dict | None:
        """Download and send one selected source in the background delivery boundary."""
        if (
            item.processing_status is not ProcessingStatus.READY
            or source.item_id != item.id
            or source.extraction_status != "READY"
            or not source.source_url
        ):
            raise RuntimeError("video source is no longer ready for delivery")

        cached_file_id = payload.get("telegram_file_id")
        cached_kind = payload.get("telegram_media_kind")
        # ❌ Удалено повторное использование generic document: file_id не подтверждает видеодорожку.
        if isinstance(cached_file_id, str) and cached_file_id and cached_kind == "video":
            try:
                return await self._upload_item_video(
                    chat_id, item, source, cached_file_id, cached_kind
                )
            except TelegramBadRequest as exc:
                if "file identifier" not in str(exc).lower():
                    raise
                log.warning(
                    "telegram rejected cached media id; downloading source again source_id=%s",
                    source.id,
                )

        if source.source_type is SourceType.YOUTUBE:
            extractor = self.youtube_extractor
        elif source.source_type is SourceType.INSTAGRAM:
            extractor = self.instagram_extractor
        else:
            raise RuntimeError("unsupported video source type")
        if extractor is None:
            raise RuntimeError("video extractor is unavailable")

        work_dir = Path(extractor.temp_dir) / f"telegram-send-{uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=False)
        try:
            try:
                if source.source_type is SourceType.YOUTUBE:
                    downloaded_path = await extractor.download_video(
                        source.source_url,
                        work_dir,
                        byte_limit=TELEGRAM_MAX_UPLOAD_BYTES,
                        include_audio=True,
                    )
                else:
                    downloaded_path = await extractor.download_video(
                        source,
                        work_dir,
                        byte_limit=TELEGRAM_MAX_UPLOAD_BYTES,
                        include_audio=True,
                    )
                path = Path(downloaded_path)
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.resolve().parent != work_dir.resolve()
                ):
                    raise AppError(
                        "DOWNLOAD_FAILED", "Downloaded video escaped its temporary directory"
                    )
                if path.stat().st_size > TELEGRAM_MAX_UPLOAD_BYTES:
                    raise MediaTooLargeError("Video exceeds Telegram's 50 MB upload limit", True)
            except AppError as exc:
                if not isinstance(exc, MediaTooLargeError):
                    raise
                if not callable(getattr(extractor, "download_audio", None)):
                    raise
                return await self._send_item_audio_fallback(
                    chat_id, item, source, extractor, work_dir
                )

            media_kind = "video" if path.suffix.lower() == ".mp4" else "document"
            try:
                return await self._upload_item_video(
                    chat_id, item, source, FSInputFile(path), media_kind
                )
            except Exception as exc:
                if not _is_telegram_upload_size_error(exc):
                    raise
                if not callable(getattr(extractor, "download_audio", None)):
                    raise MediaTooLargeError(
                        "Telegram rejected the video upload size", True
                    ) from exc
                return await self._send_item_audio_fallback(
                    chat_id, item, source, extractor, work_dir
                )
        finally:
            cleanup_temporary_directory(work_dir)

    async def _send_item_audio_fallback(
        self,
        chat_id: int,
        item: Item,
        source: ItemSource,
        extractor,
        work_dir: Path,
    ) -> None:
        """Let delivery own the user-visible fallback when only the video exceeds the send cap."""
        # Keep YouTube's shared outtmpl away from a completed or partial video in the parent dir.
        audio_work_dir = work_dir / "audio"
        audio_work_dir.mkdir(parents=True, exist_ok=True)
        try:
            if source.source_type is SourceType.YOUTUBE:
                audio_path = await extractor.download_audio(
                    source.source_url,
                    audio_work_dir,
                    byte_limit=TELEGRAM_MAX_UPLOAD_BYTES,
                )
            else:
                audio_path = await extractor.download_audio(
                    source,
                    audio_work_dir,
                    byte_limit=TELEGRAM_MAX_UPLOAD_BYTES,
                )
            path = Path(audio_path)
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != audio_work_dir.resolve()
            ):
                raise AppError(
                    "DOWNLOAD_FAILED", "Downloaded audio escaped its temporary directory"
                )
            if path.stat().st_size > TELEGRAM_MAX_UPLOAD_BYTES:
                raise MediaTooLargeError("Audio exceeds Telegram's 50 MB upload limit", True)

            source_label = (
                "YouTube" if source.source_type is SourceType.YOUTUBE else "Instagram Reel"
            )
            caption = f"Видео из {source_label} превышает лимит отправки. Отправляю только аудио."
            reply_parameters = (
                ReplyParameters(
                    message_id=item.telegram_message_id,
                    allow_sending_without_reply=True,
                )
                if item.telegram_message_id is not None
                else None
            )
            if path.suffix.lower() in {".mp3", ".m4a"}:
                await self.bot.send_audio(
                    chat_id=chat_id,
                    audio=FSInputFile(path),
                    caption=caption,
                    reply_parameters=reply_parameters,
                )
            else:
                await self.bot.send_document(
                    chat_id=chat_id,
                    document=FSInputFile(path),
                    caption=caption,
                    reply_parameters=reply_parameters,
                )
            return None
        except Exception as exc:
            is_size_error = isinstance(exc, MediaTooLargeError) or _is_telegram_upload_size_error(
                exc
            )
            permanent = is_size_error or (isinstance(exc, AppError) and exc.permanent)
            code = "AUDIO_TOO_LARGE" if is_size_error else "AUDIO_FALLBACK_FAILED"
            raise AppError(
                code,
                "audio fallback failed after video exceeded Telegram's upload size limit",
                permanent=permanent,
            ) from exc

    async def _upload_item_video(
        self,
        chat_id: int,
        item: Item,
        source: ItemSource,
        media,
        media_kind: str,
    ) -> dict | None:
        """Use Telegram's video preview for MP4 and preserve other containers as files."""
        source_label = "YouTube" if source.source_type is SourceType.YOUTUBE else "Instagram Reel"
        reply_parameters = (
            ReplyParameters(
                message_id=item.telegram_message_id,
                allow_sending_without_reply=True,
            )
            if item.telegram_message_id is not None
            else None
        )
        if media_kind == "video":
            sent = await self.bot.send_video(
                chat_id=chat_id,
                video=media,
                caption=f"Видео из {source_label}",
                supports_streaming=True,
                reply_parameters=reply_parameters,
            )
            sent_media = getattr(sent, "video", None)
        else:
            sent = await self.bot.send_document(
                chat_id=chat_id,
                document=media,
                caption=f"Видео из {source_label}",
                reply_parameters=reply_parameters,
            )
            sent_media = getattr(sent, "document", None)

        file_id = getattr(sent_media, "file_id", None)
        # ❌ Удалено кэширование generic document file_id: файл может оказаться аудио.
        if media_kind == "video" and isinstance(file_id, str) and file_id:
            return {"telegram_file_id": file_id, "telegram_media_kind": media_kind}
        return None

    async def _mark_sent(self, delivery_id: int, payload_updates: dict | None = None) -> None:
        export_artifact_name = None
        marked_sent = False
        async with self.session_factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is not None and delivery.status == "SENDING":
                if delivery.type == EXPORT_FILE:
                    payload = delivery.payload_json
                    if isinstance(payload, dict):
                        export_artifact_name = payload.get("artifact_name")
                delivery.status = "SENT"
                delivery.sent_at = func.now()
                delivery.last_error = None
                if payload_updates:
                    delivery.payload_json = {
                        **dict(delivery.payload_json or {}),
                        **payload_updates,
                    }
                marked_sent = True
            await session.commit()
        if marked_sent and export_artifact_name is not None:
            from app.services.export import remove_export_artifact

            try:
                remove_export_artifact(self.export_dir, export_artifact_name)
            except (OSError, ValueError):
                log.warning("sent export artifact cleanup failed delivery_id=%s", delivery_id)

    async def _record_failure(self, delivery_id: int, exc: Exception) -> bool:
        notify_chat_id = None
        async with self.session_factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None or delivery.status != "SENDING":
                return False
            retry = not (isinstance(exc, AppError) and exc.permanent)
            retry = retry and delivery.attempts < self.max_attempts
            delivery.status = "PENDING" if retry else "FAILED"
            delivery.last_error = str(exc)[:500]
            if not retry and delivery.type.startswith(ITEM_VIDEO_PREFIX):
                payload = dict(delivery.payload_json or {})
                payload.pop("telegram_file_id", None)
                payload.pop("telegram_media_kind", None)
                delivery.payload_json = payload
                user = await session.get(User, delivery.user_id)
                notify_chat_id = user.telegram_chat_id if user else None
            await session.commit()
        if notify_chat_id is not None:
            try:
                error_code = exc.code if isinstance(exc, AppError) else None
                if isinstance(exc, MediaTooLargeError):
                    message = "Видео превышает лимит Telegram в 50 MB. Откройте исходную ссылку."
                elif error_code == "TOO_LARGE":
                    message = "Видео превышает допустимую длительность. Откройте исходную ссылку."
                elif error_code == "AUDIO_TOO_LARGE":
                    message = (
                        "Видео превышает лимит отправки, и аудио тоже слишком большое. "
                        "Откройте исходную ссылку."
                    )
                elif error_code == "AUDIO_FALLBACK_FAILED":
                    message = (
                        "Видео превышает лимит отправки, но аудио отправить не удалось. "
                        "Попробуйте нажать кнопку позже."
                    )
                else:
                    message = "Не получилось отправить видео. Можно нажать кнопку ещё раз позже."
                await self.bot.send_message(
                    notify_chat_id,
                    message,
                )
            except Exception:
                log.exception("video delivery failure notice failed delivery_id=%s", delivery_id)
        return retry
