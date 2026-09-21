import asyncio
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import AnalysisResult, NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.audio import AudioExtractor
from app.extractors.text import TextExtractor
from app.extractors.web import WebPageExtractor
from app.extractors.youtube import YoutubeExtractor
from app.services.analysis import Analyzer
from app.services.delivery import ITEM_READY, enqueue_item_delivery
from app.services.frames import extract_representative_frames
from app.services.profile import get_profile
from app.services.retrieval import sync_item_search
from app.storage.models import Content, Item

log = logging.getLogger(__name__)


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
        visual_frame_interval_seconds: int = 20,
        visual_max_frames: int = 120,
        visual_scene_threshold: float = 0.35,
    ):
        self.analyzer = analyzer
        self.priority = priority
        self.web_extractor = web_extractor or WebPageExtractor()
        self.audio_extractor = audio_extractor
        self.youtube_extractor = youtube_extractor
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
            item.analysis_completeness = self._completeness(item, visual_notes)

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
        await enqueue_item_delivery(session, item, ITEM_READY)
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
    def _completeness(item: Item, visual_notes: str | None) -> str:
        if item.source_type in (SourceType.VOICE, SourceType.AUDIO):
            return "TRANSCRIPT_ONLY"
        if item.source_type is SourceType.YOUTUBE:
            return "TRANSCRIPT_AND_VISUAL" if visual_notes else "TRANSCRIPT_ONLY"
        return "FULL_TEXT"

    async def _extract(self, session: AsyncSession, item: Item) -> NormalizedContent:
        if item.source_type is SourceType.YOUTUBE:
            if self.youtube_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "youtube extractor not wired")
            completed_segments, on_segment = await self._transcript_checkpoints(session, item)
            content = await self.youtube_extractor.extract(
                item,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )
            # TRANSCRIPT/DESCRIPTION персистятся атомарно с checkpoint'ом
            # ANALYZING: retry не перекачивает видео и не повторяет STT (ТЗ §59).
            session.add(
                Content(
                    item_id=item.id,
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
                        kind=ContentKind.DESCRIPTION,
                        text=content.metadata["description_excerpt"],
                    )
                )
            return content
        if item.source_type in (SourceType.VOICE, SourceType.AUDIO):
            if self.audio_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "voice/audio extractor not wired")
            completed_segments, on_segment = await self._transcript_checkpoints(session, item)
            content = await self.audio_extractor.extract(
                item,
                completed_segments=completed_segments,
                on_segment=on_segment,
            )
            # TRANSCRIPT персистится атомарно с checkpoint'ом ANALYZING:
            # retry не повторяет скачивание и транскрипцию (ТЗ §59).
            session.add(
                Content(
                    item_id=item.id,
                    kind=ContentKind.TRANSCRIPT,
                    text=content.text,
                    metadata_json={"duration_seconds": item.content_duration_seconds},
                )
            )
            content.duration_seconds = item.content_duration_seconds
            return content
        if item.source_type is SourceType.WEB:
            content = await self.web_extractor.extract(item)
            # WEB_TEXT персистится атомарно с checkpoint'ом ANALYZING:
            # переживает restart, retry не перекачивает страницу (ТЗ §46, §59).
            # metadata_json хранит заголовок/автора/язык/заметку — resume
            # восстанавливает эквивалентный NormalizedContent целиком.
            session.add(
                Content(
                    item_id=item.id,
                    kind=ContentKind.WEB_TEXT,
                    text=content.text,
                    metadata_json={
                        "title": content.title,
                        "author": content.author,
                        "language": content.language,
                        "user_note": item.user_note or None,
                    },
                )
            )
            return content
        return await TextExtractor().extract(item)

    @staticmethod
    async def _transcript_checkpoints(session: AsyncSession, item: Item):
        """Expose durable per-segment STT progress without leaking DB into providers.

        OpenRouter may split long media into many requests. Each successful
        segment is committed immediately, so a later timeout/restart resumes
        from the missing indices instead of paying for completed STT again.
        """
        rows = (
            await session.scalars(
                select(Content)
                .where(
                    Content.item_id == item.id,
                    Content.kind == ContentKind.TRANSCRIPT_CHUNK,
                )
                .order_by(Content.id)
            )
        ).all()
        completed: dict[int, str] = {}
        for row in rows:
            index = (row.metadata_json or {}).get("segment_index")
            if isinstance(index, int):
                completed[index] = row.text

        checkpoint_lock = asyncio.Lock()

        async def persist(index: int, text: str) -> None:
            async with checkpoint_lock:
                if index in completed:
                    return
                session.add(
                    Content(
                        item_id=item.id,
                        kind=ContentKind.TRANSCRIPT_CHUNK,
                        text=text,
                        metadata_json={"segment_index": index},
                    )
                )
                await session.commit()
                completed[index] = text

        return completed, persist

    @staticmethod
    async def _restored_content(session: AsyncSession, item: Item) -> NormalizedContent | None:
        kind, url = ContentKind.WEB_TEXT, item.source_url
        if item.source_type in (SourceType.VOICE, SourceType.AUDIO, SourceType.YOUTUBE):
            kind, url = ContentKind.TRANSCRIPT, None
        elif item.source_type is not SourceType.WEB:
            # TEXT: извлечение тривиально, content всегда восстанавливается из заметки.
            return await TextExtractor().extract(item)
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
