import logging

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import DEFAULT_PROFILE, AnalysisResult, NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.audio import AudioExtractor
from app.extractors.text import TextExtractor
from app.extractors.web import WebPageExtractor
from app.services.analysis import Analyzer
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
    ):
        self.analyzer = analyzer
        self.priority = priority
        self.web_extractor = web_extractor or WebPageExtractor()
        self.audio_extractor = audio_extractor

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
            # Phase 2 использует default-профиль; профиль пользователя — Phase 8.
            analysis = await self.analyzer.analyze(
                content, session, item.user_id, profile=DEFAULT_PROFILE
            )

            item.processing_stage = "PRIORITIZING"
            # Дорогой результат пишется ДО checkpoint-commit: падение после
            # commit не теряет его, и retry не тянет LLM повторно.
            self._apply_analysis(item, analysis)
            await session.commit()

        item.priority_score = self.priority.score(analysis)
        item.processing_stage = "READY"
        item.processing_status = ProcessingStatus.READY
        await session.commit()
        log.info(
            "item analyzed id=%s category=%s type=%s priority=%s",
            item.id,
            item.category,
            item.item_type.value if item.item_type else None,
            item.priority_score,
        )

    async def _extract(self, session: AsyncSession, item: Item) -> NormalizedContent:
        if item.source_type in (SourceType.VOICE, SourceType.AUDIO):
            if self.audio_extractor is None:
                raise AppError("UNSUPPORTED_SOURCE", "voice/audio extractor not wired")
            content = await self.audio_extractor.extract(item)
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
    async def _restored_content(session: AsyncSession, item: Item) -> NormalizedContent | None:
        kind, url = ContentKind.WEB_TEXT, item.source_url
        if item.source_type in (SourceType.VOICE, SourceType.AUDIO):
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
        return NormalizedContent(
            source_type=item.source_type,
            title=meta.get("title"),
            text=row.text,
            url=url,
            user_note=meta.get("user_note")
            if meta.get("user_note") is not None
            else item.user_note,
            author=meta.get("author"),
            language=meta.get("language"),
        )

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
