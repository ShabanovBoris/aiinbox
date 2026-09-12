from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.llm.base import LlmProvider
from app.storage.models import Item


class Analyzer:
    """Сервис анализа: NormalizedContent + профиль + существующие категории → AnalysisResult.

    Здесь собирается персональный контекст; смена LLM-провайдера на сервис не влияет.
    """

    def __init__(self, provider: LlmProvider):
        self.provider = provider

    async def analyze(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        user_id: int,
        profile: UserProfile,
    ) -> AnalysisResult:
        categories = await existing_categories(session, user_id)
        return await self.provider.analyze(content, profile, categories)


async def existing_categories(session: AsyncSession, user_id: int) -> list[str]:
    # Категории — динамические строки, отдельная таблица не нужна (DISTINCT достаточно).
    rows = await session.scalars(
        select(Item.category).where(Item.user_id == user_id, Item.category.is_not(None)).distinct()
    )
    return sorted(rows.all())
