import asyncio
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import AnalysisResult, NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.audio import AudioExtractor
from app.extractors.document import DocumentExtractor
from app.extractors.text import TextExtractor
from app.extractors.video import VideoExtractor
from app.extractors.web import WebPageExtractor
from app.extractors.youtube import YoutubeExtractor
from app.llm.base import TranscriptionSegmentCheckpoint
from app.services.analysis import Analyzer
from app.services.delivery import ITEM_READY, enqueue_item_delivery
from app.services.frames import extract_representative_frames
from app.services.profile import get_profile
from app.services.retrieval import sync_item_search
from app.services.url_parsing import parse_message
from app.storage.models import Content, Item, ItemSource

log = logging.getLogger(__name__)
_VISUAL_ONLY_VIDEO_CONTEXT = "Транскрипт видео недоступен; анализ основан на визуальных кадрах."


class ProcessingPipeline:
    """Один канонический пайплайн: extract → analyze → prioritize → persist.

    Resumable-семантика (D-001): каждая стадия коммитится до внешнего вызова,
    дорогой LLM-результат персистится в том же commit, что ставит checkpoint
    PRIORITIZING; resume после падения продолжается с durable стадии и не
    повторяет успешный LLM-вызов. Источники добавляются в extract-шаге своих фаз.
    """

    def __init__(
        self,
        analyzer: Analyzer,
        priority: PriorityEngine,
        web_extractor: WebPageExtractor | None = None,
        audio_extractor: AudioExtractor | None = None,
        youtube_extractor: YoutubeExtractor | None = None,
        video_extractor: VideoExtractor | None = None,
        visual_frame_interval_seconds: int = 20,
        visual_max_frames: int = 120,
        visual_scene_threshold: float = 0.35,
        document_extractor: DocumentExtractor | None = None,
    ):
        self.analyzer = analyzer
        self.priority = priority
        self.web_extractor = web_extractor or WebPageExtractor()
        self.audio_extractor = audio_extractor
        self.youtube_extractor = youtube_extractor
        self.video_extractor = video_extractor
        self.document_extractor = document_extractor
        self.visual_frame_interval_seconds = visual_frame_interval_seconds
        self.visual_max_frames = visual_max_frames
        self.visual_scene_threshold = visual_scene_threshold

    async def run(self, session: AsyncSession, item: Item) -> None:
        analysis = None
        if item.processing_stage == "PRIORITIZING":
            # Checkpoint после дорогих вызовов: analysis уже персистен —
            # пересчитываем только приоритет, LLM не вызываем.
            try:
                analysis = self._restored_analysis(item)
            except ValidationError:
                analysis = None  # неполный checkpoint — честно начинаем анализ заново

        if analysis is None:
            content = None
            if item.processing_stage == "ANALYZING":
                # Resume из ANALYZING: для WEB дорогой extraction уже выполнен —
                # WEB_TEXT персистен, повторная загрузка не нужна (ТЗ §59).
                content = await self._restored_content(session, item)
            if content is None:
                item.processing_stage = "EXTRACTING"
                await session.commit()
                content = await self._extract(session, item)
            content = await self._attach_source_context(session, item, content)

            item.processing_stage = "ANALYZING"
            await session.commit()
            # Phase 7: visual enrichment — optional, graceful (ТЗ §23–24, §39).
            visual_notes = await self._visual_analysis(session, item, content)
            if visual_notes:
                content.metadata["visual_notes"] = visual_notes
            # Phase 8: персональный профиль пользователя из БД.
            profile = await get_profile(session, item.user_id)
            analysis = await self.analyzer.analyze(
                content, session, item.user_id, profile=profile, item_id=item.id
            )
            item.analysis_completeness = self._completeness(content, visual_notes)

            item.processing_stage = "PRIORITIZING"
            # Дорогой результат пишется ДО checkpoint-commit: падение после
            # commit не теряет его, и retry не тянет LLM повторно.
            self._apply_analysis(item, analysis)
            await session.commit()

        item.priority_score = self.priority.score(analysis)
        item.processing_stage = "READY"
        item.processing_status = ProcessingStatus.READY
        # Индекс — производная проекция; обновляется в том же commit, что и READY,
        # чтобы новый результат не появлялся в поиске без основного Item.
        await sync_item_search(session, item.id)
        # Delivery intent входит в тот же commit, что READY: падение процесса
        # после commit больше не создаёт окно безвозвратной потери уведомления.
        # Reopen is needed when a READY/PARTIAL Item is explicitly retried after
        # a child source recovers; the same durable delivery key then publishes
        # the newly synthesized result once instead of suppressing it as a replay.
        await enqueue_item_delivery(session, item, ITEM_READY, reopen=True)
        await session.commit()
        log.info(
            "item analyzed id=%s category=%s type=%s priority=%s",
            item.id,
            item.category,
            item.item_type.value if item.item_type else None,
            item.priority_score,
        )

    async def _visual_analysis(
        self, session: AsyncSession, item: Item, content: NormalizedContent
    ) -> str | None:
        """Phase 7: representative frames → vision → compact visual notes.

        Graceful по ТЗ §39/§24: нет vision-capability / ffmpeg / ошибка — Item
        продолжается по транскрипту с completeness=TRANSCRIPT_ONLY."""
        existing = content.metadata.get("visual_notes")
        if existing:
            # visual notes уже персистены и восстановлены при resume —
            # повторный download/ffmpeg/vision не нужен (AGENTS §18)
            return existing
        persisted = await session.scalar(
            select(Content.text)
            .where(Content.item_id == item.id, Content.kind == ContentKind.VISUAL_NOTES)
            .order_by(Content.id.desc())
        )
        if persisted:
            return persisted
        capabilities = getattr(self.analyzer.provider, "capabilities", None)
        if not capabilities or not capabilities.vision:
            return None
        if item.source_type is not SourceType.YOUTUBE or self.youtube_extractor is None:
            return None
        work_dir = Path(self.youtube_extractor.temp_dir) / f"vis-{uuid4().hex}"
        try:
            # mkdir внутри graceful-границы: FS-ошибка не роняет Item с транскриптом
            work_dir.mkdir(parents=True, exist_ok=True)
            video = await self.youtube_extractor.download_video(item.source_url, work_dir)
            # ffmpeg — синхронный subprocess: выполняется в thread, не блокируя loop
            frames = await asyncio.to_thread(
                extract_representative_frames,
                video,
                work_dir / "frames",
                interval_seconds=self.visual_frame_interval_seconds,
                max_frames=self.visual_max_frames,
                scene_threshold=self.visual_scene_threshold,
                duration_seconds=content.duration_seconds or item.content_duration_seconds,
            )
            if not frames:
                return None
            notes = await self.analyzer.provider.describe_images(
                frames, context=content.text[:1500]
            )
            # Обрезка до детерминированного лимита: untrusted LLM output
            notes = notes[:800]
            if notes:
                # Успешный vision персистится ДО Analyzer (AGENTS §18):
                # retry из ANALYZING не повторяет download/ffmpeg/vision.
                session.add(
                    Content(
                        item_id=item.id,
                        kind=ContentKind.VISUAL_NOTES,
                        text=notes,
                        metadata_json={"frames": len(frames)},
                    )
                )
                await session.commit()
                return notes
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # vision failure не роняет Item с валидным транскриптом (ТЗ §39)
            log.warning("visual analysis skipped item_id=%s: %s", item.id, exc)
            return None
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _completeness(content: NormalizedContent, visual_notes: str | None) -> str:
        if content.metadata.get("source_failures"):
            return "PARTIAL"

        visual_only_sources = int(content.metadata.get("visual_only_source_count") or 0)
        if visual_only_sources:
            successful_source_count = int(content.metadata.get("successful_source_count") or 0)
            return "VISUAL_ONLY" if visual_only_sources == successful_source_count else "PARTIAL"

        # ❌ Удалена зависимость completeness от parent Item.source_type: composite
        # Item может быть TEXT при реальных child sources WEB + YOUTUBE/VIDEO.
        source_types = set(content.metadata.get("successful_source_types") or [])
        if not source_types:
            source_types = {content.source_type.value}
        transcript_types = {
            SourceType.VOICE.value,
            SourceType.AUDIO.value,
            SourceType.YOUTUBE.value,
            SourceType.VIDEO.value,
        }
        if source_types & transcript_types:
            visual_source_count = int(content.metadata.get("visual_source_count") or 0)
            visual_with_notes = int(content.metadata.get("visual_source_count_with_notes") or 0)
            if not content.metadata.get("successful_source_types") and content.source_type in (
                SourceType.YOUTUBE,
                SourceType.VIDEO,
            ):
                visual_source_count = 1
                visual_with_notes = int(bool(visual_notes or content.metadata.get("visual_notes")))
            if visual_source_count and visual_with_notes == visual_source_count:
                return "TRANSCRIPT_AND_VISUAL"
            return "TRANSCRIPT_ONLY"
        return "FULL_TEXT"

    async def _extract(self, session: AsyncSession, item: Item) -> NormalizedContent:
        sources = await self._item_sources(session, item.id)
        if sources:
            return await self._extract_item_sources(session, item, sources)
        return await self._extract_legacy_source(session, item)

    async def _extract_item_sources(
        self, session: AsyncSession, item: Item, sources: list[ItemSource]
    ) -> NormalizedContent:
        """Extract child sources independently and keep usable siblings on source failure."""
        message_text = await self._stored_source_text(session, item.id)
        extracted: list[NormalizedContent] = []
        failures: list[dict[str, str | int]] = []

        for source in sources:
            if source.extraction_status == "FAILED":
                failures.append(self._source_failure(source))
                continue
            content = await self._restored_source_content(session, item, source)
            if content is None:
                try:
                    content = await self._extract_source(session, item, source, source.id)
                except AppError as exc:
                    source.extraction_status = "FAILED"
                    source.error_code = exc.code
                    source.error_message = str(exc)[:500]
                    source.metadata_json = {
                        **(source.metadata_json or {}),
                        "failure_permanent": exc.permanent,
                    }
                    await session.commit()
                    failures.append(self._source_failure(source))
                    log.warning(
                        "item source extraction failed item_id=%s source_id=%s "
                        "source_type=%s error_code=%s",
                        item.id,
                        source.id,
                        source.source_type.value,
                        exc.code,
                    )
                    continue
                source.extraction_status = "READY"
                source.error_code = None
                source.error_message = None
                # Each successful extraction is a durable checkpoint before the
                # next independent source starts, so later failures/restart do not
                # repeat already completed network/STT work.
                await session.commit()
            extracted.append(content)

        fallback_text = self._meaningful_message_text(item, message_text)
        if not extracted and fallback_text is None:
            if failures:
                first = failures[0]
                raise AppError(
                    str(first.get("error_code") or "EXTRACTION_FAILED"),
                    str(first.get("error_message") or "all item sources failed"),
                )
            raise AppError("EXTRACTION_FAILED", "item contains no analyzable content")
        return self._compose_item_content(item, message_text, sources, extracted, failures)

    async def _extract_legacy_source(self, session: AsyncSession, item: Item) -> NormalizedContent:
        """Keep pre-ItemSource rows and focused extractor tests compatible during migration."""
        if item.source_type is SourceType.TEXT:
            return await self._text_content(session, item)
        return await self._extract_source(session, item, item, None)

    async def _extract_source(
        self,
        session: AsyncSession,
        item: Item,
        source: Item | ItemSource,
        source_id: int | None,
    ) -> NormalizedContent:
        """Run one source adapter; parent Item owns the eventual combined analysis."""
        if source.source_type is SourceType.YOUTUBE:
            if self.youtube_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "youtube extractor not wired")
            completed_segments, on_segment = await self._transcript_checkpoints(
                session, item, source_id
            )
            content = await self.youtube_extractor.extract(
                source,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )
            # TRANSCRIPT/DESCRIPTION персистятся атомарно с checkpoint'ом
            # ANALYZING: retry не перекачивает видео и не повторяет STT (ТЗ §59).
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source_id,
                    kind=ContentKind.TRANSCRIPT,
                    text=content.text,
                    metadata_json={
                        "duration_seconds": content.duration_seconds,
                        "via_stt": content.metadata.get("via_stt"),
                        "title": content.title,
                        "canonical_url": content.url,
                        "cues": content.metadata.get("cues"),
                    },
                )
            )
            if content.metadata.get("description_excerpt"):
                session.add(
                    Content(
                        item_id=item.id,
                        source_id=source_id,
                        kind=ContentKind.DESCRIPTION,
                        text=content.metadata["description_excerpt"],
                    )
                )
            await self._delete_transcript_checkpoints(session, item.id, source_id)
            await self._enrich_source_visual(session, item, source, content, source_id)
            return content
        if source.source_type is SourceType.VIDEO:
            if self.video_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "video extractor not wired")
            completed_segments, on_segment = await self._transcript_checkpoints(
                session, item, source_id
            )
            try:
                content = await self.video_extractor.extract(
                    source,
                    completed_segments=completed_segments,
                    on_segment=on_segment,
                )
            except AppError as exc:
                if (
                    exc.code not in {"NO_AUDIO_TRACK", "EMPTY_TRANSCRIPT"}
                    or source_id is None
                    or not isinstance(source, ItemSource)
                ):
                    raise
                # Without a transcript, durable frame notes become the source checkpoint.
                content = NormalizedContent(
                    source_type=SourceType.VIDEO,
                    text=_VISUAL_ONLY_VIDEO_CONTEXT,
                    duration_seconds=source.content_duration_seconds,
                    metadata={
                        "visual_only": True,
                        "transcript_error_code": exc.code,
                    },
                )
                await self._enrich_source_visual(session, item, source, content, source_id)
                if not content.metadata.get("visual_notes"):
                    raise
                source.metadata_json = {
                    **(source.metadata_json or {}),
                    "video_visual_only": True,
                    "video_transcript_error_code": exc.code,
                }
                log.info(
                    "video source continues with visual-only content item_id=%s "
                    "source_id=%s transcript_error_code=%s",
                    item.id,
                    source.id,
                    exc.code,
                )
                return content
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source_id,
                    kind=ContentKind.TRANSCRIPT,
                    text=content.text,
                    metadata_json={"duration_seconds": source.content_duration_seconds},
                )
            )
            await self._delete_transcript_checkpoints(session, item.id, source_id)
            if isinstance(source, ItemSource) and (source.metadata_json or {}).get(
                "video_visual_only"
            ):
                source_metadata = dict(source.metadata_json or {})
                source_metadata.pop("video_visual_only", None)
                source_metadata.pop("video_transcript_error_code", None)
                source.metadata_json = source_metadata
            await self._enrich_source_visual(session, item, source, content, source_id)
            return content
        if source.source_type in (SourceType.VOICE, SourceType.AUDIO):
            if self.audio_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "voice/audio extractor not wired")
            completed_segments, on_segment = await self._transcript_checkpoints(
                session, item, source_id
            )
            content = await self.audio_extractor.extract(
                source,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )
            # TRANSCRIPT персистится атомарно с checkpoint'ом ANALYZING:
            # retry не повторяет скачивание и транскрипцию (ТЗ §59).
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source_id,
                    kind=ContentKind.TRANSCRIPT,
                    text=content.text,
                    metadata_json={"duration_seconds": source.content_duration_seconds},
                )
            )
            await self._delete_transcript_checkpoints(session, item.id, source_id)
            content.duration_seconds = source.content_duration_seconds
            return content
        if source.source_type is SourceType.DOCUMENT:
            if self.document_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "document extractor not wired", permanent=True)
            content = await self.document_extractor.extract(source)
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source_id,
                    kind=ContentKind.DOCUMENT_TEXT,
                    text=content.text,
                    metadata_json={**content.metadata, "title": content.title},
                )
            )
            source.metadata_json = {**(source.metadata_json or {}), **content.metadata}
            return content
        if source.source_type is SourceType.WEB:
            content = await self.web_extractor.extract(source)
            # WEB_TEXT персистится атомарно с checkpoint'ом ANALYZING:
            # переживает restart, retry не перекачивает страницу (ТЗ §46, §59).
            # metadata_json хранит заголовок/автора/язык/заметку — resume
            # восстанавливает эквивалентный NormalizedContent целиком.
            is_document = content.source_type is SourceType.DOCUMENT
            content_metadata = (
                {**content.metadata, "title": content.title}
                if is_document
                else {
                    "title": content.title,
                    "author": content.author,
                    "language": content.language,
                    "user_note": None,
                }
            )
            session.add(
                Content(
                    item_id=item.id,
                    source_id=source_id,
                    kind=ContentKind.DOCUMENT_TEXT if is_document else ContentKind.WEB_TEXT,
                    text=content.text,
                    metadata_json=content_metadata,
                )
            )
            if is_document and isinstance(source, ItemSource):
                source.metadata_json = {**(source.metadata_json or {}), **content.metadata}
            return content
        raise AppError("UNSUPPORTED_SOURCE", f"unsupported source type: {source.source_type.value}")

    async def _enrich_source_visual(
        self,
        session: AsyncSession,
        item: Item,
        source: ItemSource,
        content: NormalizedContent,
        source_id: int | None,
    ) -> None:
        """Attach optional visual facts to one video-like source without owning Item failure."""
        if source_id is None:
            return
        existing = await session.scalar(
            select(Content.text).where(
                Content.item_id == item.id,
                Content.source_id == source_id,
                Content.kind == ContentKind.VISUAL_NOTES,
            )
        )
        if existing:
            content.metadata["visual_notes"] = existing
            return
        capabilities = getattr(self.analyzer.provider, "capabilities", None)
        if not capabilities or not capabilities.vision:
            return

        temp_root = None
        if source.source_type is SourceType.YOUTUBE and self.youtube_extractor is not None:
            temp_root = Path(self.youtube_extractor.temp_dir)
        elif source.source_type is SourceType.VIDEO and self.video_extractor is not None:
            temp_root = Path(self.video_extractor.temp_dir)
        if temp_root is None:
            return

        work_dir = temp_root / f"vis-source-{source_id}-{uuid4().hex}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            if source.source_type is SourceType.YOUTUBE:
                video = await self.youtube_extractor.download_video(source.source_url, work_dir)
            else:
                video = await self.video_extractor.download_video(source, work_dir)
            frames = await asyncio.to_thread(
                extract_representative_frames,
                video,
                work_dir / "frames",
                interval_seconds=self.visual_frame_interval_seconds,
                max_frames=self.visual_max_frames,
                scene_threshold=self.visual_scene_threshold,
                duration_seconds=content.duration_seconds or source.content_duration_seconds,
            )
            if not frames:
                return
            notes = await self.analyzer.provider.describe_images(
                frames, context=content.text[:1500]
            )
            notes = notes[:800]
            if notes:
                content.metadata["visual_notes"] = notes
                session.add(
                    Content(
                        item_id=item.id,
                        source_id=source_id,
                        kind=ContentKind.VISUAL_NOTES,
                        text=notes,
                        metadata_json={"frames": len(frames)},
                    )
                )
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Callers decide whether another extracted part keeps the source useful.
            log.warning(
                "source visual analysis skipped item_id=%s source_id=%s: %s",
                item.id,
                source_id,
                exc,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    async def _item_sources(session: AsyncSession, item_id: int) -> list[ItemSource]:
        """Load child sources in deterministic message-local order."""
        return list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item_id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )

    @staticmethod
    def _source_failure(source: ItemSource) -> dict[str, str | int]:
        """Expose bounded failure facts to completeness/analysis without provider exceptions."""
        return {
            "source_index": source.source_index,
            "source_type": source.source_type.value,
            "error_code": source.error_code or "EXTRACTION_FAILED",
            "error_message": (source.error_message or "source extraction failed")[:500],
        }

    @staticmethod
    def _meaningful_message_text(item: Item, message_text: str | None) -> str | None:
        """Return text that can honestly carry a partial analysis when sources fail."""
        if not message_text or not message_text.strip():
            return None
        if item.user_note and item.user_note.strip():
            return message_text
        if (item.source_metadata_json or {}).get("forwarded") is True:
            note, _ = parse_message(message_text)
            return message_text if note.strip() else None
        return None

    @staticmethod
    def _compose_item_content(
        item: Item,
        message_text: str | None,
        sources: list[ItemSource],
        extracted: list[NormalizedContent],
        failures: list[dict[str, str | int]],
    ) -> NormalizedContent:
        """Combine all successful source payloads into one Analyzer input for the Item."""
        if not extracted:
            return NormalizedContent(
                source_type=SourceType.TEXT,
                text=message_text or item.user_note,
                metadata={
                    "source_count": len(sources),
                    "successful_source_count": 0,
                    "successful_source_types": [],
                    "source_failures": failures,
                    "visual_source_count": 0,
                    "visual_source_count_with_notes": 0,
                },
            )

        single = extracted[0] if len(extracted) == 1 else None
        if single is not None:
            combined_text = single.text
        else:
            sections: list[str] = []
            for index, content in enumerate(extracted, start=1):
                header = f"SOURCE {index} [{content.source_type.value}]"
                if content.url:
                    header += f" {content.url}"
                lines = [header]
                if content.title:
                    lines.append(f"Title: {content.title}")
                if content.author:
                    lines.append(f"Author: {content.author}")
                description = content.metadata.get("description_excerpt")
                if description:
                    lines.append(f"Description: {description}")
                visual_notes = content.metadata.get("visual_notes")
                if visual_notes:
                    lines.append(f"Visual notes: {visual_notes}")
                lines.append(content.text)
                sections.append("\n".join(lines))
            combined_text = "\n\n".join(sections)

        forwarded = (item.source_metadata_json or {}).get("forwarded") is True
        visual_sources = [
            content
            for content in extracted
            if content.source_type in (SourceType.YOUTUBE, SourceType.VIDEO)
        ]
        visual_only_source_count = sum(
            bool(content.metadata.get("visual_only")) for content in visual_sources
        )
        combined_metadata = dict(single.metadata) if single is not None else {}
        combined_metadata.update(
            {
                "source_count": len(sources),
                "successful_source_count": len(extracted),
                "successful_source_types": [content.source_type.value for content in extracted],
                "source_failures": failures,
                "visual_source_count": len(visual_sources),
                "visual_only_source_count": visual_only_source_count,
                "visual_source_count_with_notes": sum(
                    bool(content.metadata.get("visual_notes")) for content in visual_sources
                ),
            }
        )
        return NormalizedContent(
            source_type=single.source_type if single else item.source_type,
            title=single.title if single else None,
            text=combined_text,
            url=single.url if single else None,
            user_note=(item.user_note or None) if not forwarded else None,
            source_context=message_text if forwarded and message_text else None,
            author=single.author if single else None,
            language=single.language if single else None,
            duration_seconds=single.duration_seconds if single else None,
            metadata=combined_metadata,
        )

    @staticmethod
    async def _delete_transcript_checkpoints(
        session: AsyncSession, item_id: int, source_id: int | None = None
    ) -> None:
        """Remove STT work-in-progress rows only after a final transcript exists.

        This runs in the caller's final-transcript transaction: rollback keeps
        durable chunks for retry, while a successful commit leaves one canonical
        transcript for retrieval/FTS instead of indexing both chunks and aggregate.
        """
        where = [Content.item_id == item_id, Content.kind == ContentKind.TRANSCRIPT_CHUNK]
        where.append(
            Content.source_id == source_id if source_id is not None else Content.source_id.is_(None)
        )
        await session.execute(delete(Content).where(*where))

    @staticmethod
    async def _transcript_checkpoints(
        session: AsyncSession, item: Item, source_id: int | None = None
    ):
        """Expose durable per-segment STT progress without leaking DB into providers.

        OpenRouter may split long media into many requests. Each successful
        segment is committed immediately, so a later timeout/restart resumes
        from the missing indices instead of paying for completed STT again.
        """
        where = [Content.item_id == item.id, Content.kind == ContentKind.TRANSCRIPT_CHUNK]
        where.append(
            Content.source_id == source_id if source_id is not None else Content.source_id.is_(None)
        )
        rows = (await session.scalars(select(Content).where(*where).order_by(Content.id))).all()
        completed: dict[int, TranscriptionSegmentCheckpoint] = {}
        for row in rows:
            meta = row.metadata_json or {}
            index = meta.get("segment_index")
            if not isinstance(index, int):
                continue
            try:
                completed[index] = TranscriptionSegmentCheckpoint(
                    text=row.text,
                    input_sha256=meta["input_sha256"],
                    provider=meta["provider"],
                    model=meta["model"],
                    segment_seconds=meta["segment_seconds"],
                    format_version=meta["format_version"],
                )
            except (KeyError, TypeError):
                # Legacy index-only checkpoints are deliberately not reusable.
                continue

        checkpoint_lock = asyncio.Lock()

        async def persist(index: int, checkpoint: TranscriptionSegmentCheckpoint) -> None:
            async with checkpoint_lock:
                if completed.get(index) == checkpoint:
                    return
                session.add(
                    Content(
                        item_id=item.id,
                        source_id=source_id,
                        kind=ContentKind.TRANSCRIPT_CHUNK,
                        text=checkpoint.text,
                        metadata_json={
                            "segment_index": index,
                            "input_sha256": checkpoint.input_sha256,
                            "provider": checkpoint.provider,
                            "model": checkpoint.model,
                            "segment_seconds": checkpoint.segment_seconds,
                            "format_version": checkpoint.format_version,
                        },
                    )
                )
                await session.commit()
                completed[index] = checkpoint

        return completed, persist

    @staticmethod
    async def _stored_source_text(session: AsyncSession, item_id: int) -> str | None:
        """Read source-authored Telegram text from the existing durable content store."""
        return await session.scalar(
            select(Content.text)
            .where(Content.item_id == item_id, Content.kind == ContentKind.USER_TEXT)
            .order_by(Content.id)
        )

    @staticmethod
    async def _text_content(session: AsyncSession, item: Item) -> NormalizedContent:
        """Use persisted forwarded text as TEXT content while preserving legacy TEXT behavior."""
        source_text = await ProcessingPipeline._stored_source_text(session, item.id)
        if source_text is not None:
            return NormalizedContent(source_type=SourceType.TEXT, text=source_text)
        return await TextExtractor().extract(item)

    @staticmethod
    async def _attach_source_context(
        session: AsyncSession, item: Item, content: NormalizedContent
    ) -> NormalizedContent:
        """Attach forwarded source text to URL/media without turning it into user intent."""
        metadata = item.source_metadata_json or {}
        if item.source_type is SourceType.TEXT or metadata.get("forwarded") is not True:
            return content
        source_text = await ProcessingPipeline._stored_source_text(session, item.id)
        if source_text:
            content.source_context = source_text
        return content

    @staticmethod
    async def _restored_content(session: AsyncSession, item: Item) -> NormalizedContent | None:
        sources = await ProcessingPipeline._item_sources(session, item.id)
        if sources:
            return await ProcessingPipeline._restored_item_sources(session, item, sources)
        return await ProcessingPipeline._restored_legacy_content(session, item)

    @staticmethod
    async def _restored_item_sources(
        session: AsyncSession, item: Item, sources: list[ItemSource]
    ) -> NormalizedContent | None:
        """Restore a fully extracted composite Item without repeating external work."""
        if any(source.extraction_status == "PENDING" for source in sources):
            return None
        extracted: list[NormalizedContent] = []
        failures: list[dict[str, str | int]] = []
        for source in sources:
            if source.extraction_status == "FAILED":
                failures.append(ProcessingPipeline._source_failure(source))
                continue
            content = await ProcessingPipeline._restored_source_content(session, item, source)
            if content is None:
                return None
            extracted.append(content)
        message_text = await ProcessingPipeline._stored_source_text(session, item.id)
        if (
            not extracted
            and ProcessingPipeline._meaningful_message_text(item, message_text) is None
        ):
            return None
        return ProcessingPipeline._compose_item_content(
            item, message_text, sources, extracted, failures
        )

    @staticmethod
    async def _restored_source_content(
        session: AsyncSession, item: Item, source: ItemSource
    ) -> NormalizedContent | None:
        """Restore one source checkpoint by durable source id."""
        kind = ContentKind.WEB_TEXT
        if source.source_type in (
            SourceType.VOICE,
            SourceType.AUDIO,
            SourceType.YOUTUBE,
            SourceType.VIDEO,
        ):
            kind = ContentKind.TRANSCRIPT
        elif source.source_type is SourceType.DOCUMENT:
            kind = ContentKind.DOCUMENT_TEXT
        elif source.source_type is not SourceType.WEB:
            return None
        source_content_kind = (
            Content.kind.in_((ContentKind.WEB_TEXT, ContentKind.DOCUMENT_TEXT))
            if source.source_type is SourceType.WEB
            else Content.kind == kind
        )
        query = select(Content).where(
            Content.item_id == item.id,
            Content.source_id == source.id,
            source_content_kind,
        )
        row = await session.scalar(query.order_by(Content.id.desc()))
        if row is None:
            if source.source_type is SourceType.VIDEO and (source.metadata_json or {}).get(
                "video_visual_only"
            ):
                visual_notes = await session.scalar(
                    select(Content.text).where(
                        Content.item_id == item.id,
                        Content.source_id == source.id,
                        Content.kind == ContentKind.VISUAL_NOTES,
                    )
                )
                if visual_notes:
                    return NormalizedContent(
                        source_type=SourceType.VIDEO,
                        text=_VISUAL_ONLY_VIDEO_CONTEXT,
                        duration_seconds=source.content_duration_seconds,
                        metadata={
                            "visual_only": True,
                            "transcript_error_code": (source.metadata_json or {}).get(
                                "video_transcript_error_code"
                            ),
                            "visual_notes": visual_notes,
                        },
                    )
            return None
        meta = row.metadata_json or {}
        restored_source_type = (
            SourceType.DOCUMENT if row.kind is ContentKind.DOCUMENT_TEXT else source.source_type
        )
        content = NormalizedContent(
            source_type=restored_source_type,
            title=meta.get("title"),
            text=row.text,
            url=source.source_url if source.source_type is SourceType.WEB else None,
            duration_seconds=meta.get("duration_seconds"),
            author=meta.get("author"),
            language=meta.get("language"),
            metadata=(
                {key: value for key, value in meta.items() if key != "title"}
                if row.kind is ContentKind.DOCUMENT_TEXT
                else {}
            ),
        )
        if source.source_type is SourceType.YOUTUBE:
            description_row = await session.scalar(
                select(Content).where(
                    Content.item_id == item.id,
                    Content.source_id == source.id,
                    Content.kind == ContentKind.DESCRIPTION,
                )
            )
            content.url = meta.get("canonical_url") or source.source_url
            content.metadata = {
                "description_excerpt": description_row.text if description_row else None,
                "via_stt": meta.get("via_stt"),
                "cues": meta.get("cues"),
            }
        if source.source_type in (SourceType.YOUTUBE, SourceType.VIDEO):
            visual_row = await session.scalar(
                select(Content).where(
                    Content.item_id == item.id,
                    Content.source_id == source.id,
                    Content.kind == ContentKind.VISUAL_NOTES,
                )
            )
            if visual_row is not None:
                content.metadata["visual_notes"] = visual_row.text
        return content

    @staticmethod
    async def _restored_legacy_content(
        session: AsyncSession, item: Item
    ) -> NormalizedContent | None:
        kind, url = ContentKind.WEB_TEXT, item.source_url
        if item.source_type in (SourceType.VOICE, SourceType.AUDIO, SourceType.YOUTUBE):
            kind, url = ContentKind.TRANSCRIPT, None
        elif item.source_type is not SourceType.WEB:
            # Forwarded TEXT lives in contents; ordinary TEXT keeps legacy user_note storage.
            return await ProcessingPipeline._text_content(session, item)
        row = await session.scalar(
            select(Content).where(Content.item_id == item.id, Content.kind == kind)
        )
        if row is None:
            return None
        meta = row.metadata_json or {}
        content = NormalizedContent(
            source_type=item.source_type,
            title=meta.get("title"),
            text=row.text,
            url=url,
            duration_seconds=meta.get("duration_seconds"),
            user_note=meta.get("user_note") or (item.user_note or None),
            author=meta.get("author"),
            language=meta.get("language"),
        )
        if item.source_type is SourceType.YOUTUBE:
            # checkpoint восстанавливает эквивалентный NormalizedContent:
            # канонический url, описание и cues персистятся вместе с транскриптом.
            description_row = await session.scalar(
                select(Content).where(
                    Content.item_id == item.id, Content.kind == ContentKind.DESCRIPTION
                )
            )
            content.url = meta.get("canonical_url") or item.source_url
            visual_row = await session.scalar(
                select(Content).where(
                    Content.item_id == item.id, Content.kind == ContentKind.VISUAL_NOTES
                )
            )
            content.metadata = {
                "description_excerpt": description_row.text if description_row else None,
                "via_stt": meta.get("via_stt"),
                "cues": meta.get("cues"),
            }
            if visual_row is not None:
                # ключ добавляется только при наличии записи — эквивалентность
                # initial/resumed content (первый прогон не имеет ключа)
                content.metadata["visual_notes"] = visual_row.text
        return content

    @staticmethod
    def _restored_analysis(item: Item) -> AnalysisResult:
        return AnalysisResult(
            title=item.title,
            summary=item.summary or "",
            category=item.category or "",
            item_type=item.item_type,
            tags=item.tags_json or [],
            importance=item.importance or 0.0,
            urgency=item.urgency or 0.0,
            goal_fit=item.goal_fit or 0.0,
            long_term_value=item.long_term_value or 0.0,
            interest_fit=item.interest_fit or 0.0,
            estimated_action_minutes=item.estimated_action_minutes,
            next_action=item.next_action,
            suggested_due_at=item.suggested_due_at,
            priority_reason=item.priority_reason or "",
            language=item.language or "ru",
            confidence=item.confidence or 0.0,
        )

    @staticmethod
    def _apply_analysis(item: Item, analysis: AnalysisResult) -> None:
        item.title = analysis.title
        item.summary = analysis.summary
        item.category = analysis.category
        item.item_type = analysis.item_type
        item.tags_json = analysis.tags
        item.importance = analysis.importance
        item.urgency = analysis.urgency
        item.goal_fit = analysis.goal_fit
        item.long_term_value = analysis.long_term_value
        item.interest_fit = analysis.interest_fit
        item.estimated_action_minutes = analysis.estimated_action_minutes
        item.next_action = analysis.next_action
        item.suggested_due_at = analysis.suggested_due_at
        item.priority_reason = analysis.priority_reason
        item.language = analysis.language
        item.confidence = analysis.confidence
