from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.llm.base import LlmProvider
from app.storage.models import Item


def split_text(text: str, chunk_size_chars: int) -> list[str]:
    """Split text into contiguous chunks without dropping or reordering bytes."""
    if chunk_size_chars <= 0:
        raise ValueError("chunk_size_chars must be positive")
    return [
        text[start : start + chunk_size_chars] for start in range(0, len(text), chunk_size_chars)
    ]


class Analyzer:
    """Сервис анализа: NormalizedContent + профиль + существующие категории → AnalysisResult.

    Здесь собирается персональный контекст; смена LLM-провайдера на сервис не влияет.
    """

    def __init__(self, provider: LlmProvider, chunk_size_chars: int = 12_000):
        self.provider = provider
        # Chunking belongs to the application boundary: providers summarize
        # bounded fragments, while the final classification stays one call.
        self.chunk_size_chars = chunk_size_chars

    async def analyze(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        user_id: int,
        profile: UserProfile,
    ) -> AnalysisResult:
        if len(content.text) > self.chunk_size_chars:
            chunks = split_text(content.text, self.chunk_size_chars)
            summaries = [await self.provider.summarize_chunk(chunk) for chunk in chunks]
            content = content.model_copy(update={"text": "\n\n".join(summaries)})
        categories = await existing_categories(session, user_id)
        return await self.provider.analyze(content, profile, categories)


async def existing_categories(session: AsyncSession, user_id: int) -> list[str]:
    # Категории — динамические строки, отдельная таблица не нужна (DISTINCT достаточно).
    rows = await session.scalars(
        select(Item.category).where(Item.user_id == user_id, Item.category.is_not(None)).distinct()
    )
    return sorted(rows.all())
