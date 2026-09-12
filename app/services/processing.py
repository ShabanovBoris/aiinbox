import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DEFAULT_PROFILE
from app.domain.priority import PriorityEngine
from app.extractors.text import TextExtractor
from app.services.analysis import Analyzer
from app.storage.models import Item

log = logging.getLogger(__name__)


class ProcessingPipeline:
    """Один канонический пайплайн: extract → analyze → prioritize → persist.

    Стадии пишутся в processing_stage, чтобы при сбое видеть, где остановились
    (D-001 resumable); источники добавляются в extract-шаге своих фаз.
    """

    def __init__(self, analyzer: Analyzer, priority: PriorityEngine):
        self.analyzer = analyzer
        self.priority = priority

    async def run(self, session: AsyncSession, item: Item) -> None:
        item.processing_stage = "EXTRACTING"
        content = await TextExtractor().extract(item)

        item.processing_stage = "ANALYZING"
        # Phase 2 использует default-профиль; профиль пользователя — Phase 8.
        analysis = await self.analyzer.analyze(
            content, session, item.user_id, profile=DEFAULT_PROFILE
        )

        item.processing_stage = "PRIORITIZING"
        self._apply_analysis(item, analysis)
        item.priority_score = self.priority.score(analysis)

        item.processing_stage = "READY"
        log.info(
            "item analyzed id=%s category=%s type=%s priority=%s",
            item.id,
            item.category,
            item.item_type.value if item.item_type else None,
            item.priority_score,
        )

    @staticmethod
    def _apply_analysis(item: Item, analysis) -> None:
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
