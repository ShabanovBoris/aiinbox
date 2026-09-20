import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind
from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.llm.base import LlmError, LlmProvider
from app.storage.models import Content, Item


def split_text(text: str, chunk_size_chars: int, overlap_chars: int = 0) -> list[str]:
    """Split into bounded chunks while preferring complete paragraph boundaries."""
    if chunk_size_chars <= 0 or not 0 <= overlap_chars < chunk_size_chars:
        raise ValueError("chunk_size_chars must be positive and overlap smaller than chunk size")
    if not text:
        return []
    # ❌ Удален fixed-step character slicing: он игнорировал требуемые PRODUCT_SPEC
    # paragraph boundaries и разрывал абзац даже когда рядом был безопасный split.
    chunks = []
    start = 0
    while start < len(text):
        hard_end = min(start + chunk_size_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            # Do not cut inside the repeated overlap from the previous chunk;
            # the chosen boundary must add fresh content before advancing.
            boundary_from = start + (overlap_chars if chunks else 0)
            paragraph_end = text.rfind("\n\n", boundary_from + 1, hard_end)
            if paragraph_end >= 0:
                end = paragraph_end + 2

        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap_chars
    return chunks


class Analyzer:
    """Сервис анализа: NormalizedContent + профиль + существующие категории → AnalysisResult.

    Здесь собирается персональный контекст; смена LLM-провайдера на сервис не влияет.
    """

    def __init__(
        self, provider: LlmProvider, chunk_size_chars: int = 12_000, overlap_chars: int = 0
    ):
        self.provider = provider
        # Chunking belongs to the application boundary: providers summarize
        # bounded fragments, while the final classification stays one call.
        self.chunk_size_chars = chunk_size_chars
        self.overlap_chars = overlap_chars

    async def analyze(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        user_id: int,
        profile: UserProfile,
        item_id: int | None = None,
    ) -> AnalysisResult:
        if len(content.text) > self.chunk_size_chars:
            if item_id is None:
                raise ValueError("item_id is required for durable long-content analysis")
            chunks = split_text(content.text, self.chunk_size_chars, self.overlap_chars)
            summaries = await self._durable_chunk_summaries(session, item_id, chunks)
            content = content.model_copy(update={"text": self._bounded_aggregate(summaries)})
        categories = await existing_categories(session, user_id)
        return await self.provider.analyze(content, profile, categories)

    async def _durable_chunk_summaries(
        self, session: AsyncSession, item_id: int, chunks: list[str]
    ) -> list[str]:
        """Persist each expensive summary before requesting final analysis."""
        rows = (
            await session.scalars(
                select(Content).where(
                    Content.item_id == item_id,
                    Content.kind == ContentKind.CHUNK_SUMMARY,
                )
            )
        ).all()
        stored = {}
        for row in rows:
            metadata = row.metadata_json or {}
            if (
                metadata.get("stage") == "chunk"
                and metadata.get("chunk_size_chars") == self.chunk_size_chars
                and metadata.get("overlap_chars") == self.overlap_chars
                and metadata.get("chunk_sha256")
            ):
                stored[(metadata["chunk_index"], metadata["chunk_sha256"])] = row.text

        summaries = []
        for index, chunk in enumerate(chunks):
            chunk_sha256 = hashlib.sha256(chunk.encode()).hexdigest()
            summary = stored.get((index, chunk_sha256))
            if summary is None:
                summary = (await self.provider.summarize_chunk(chunk)).strip()
                if not summary:
                    raise LlmError("INVALID_LLM_OUTPUT", "empty chunk summary")
                summary = summary[: self.chunk_size_chars]
                session.add(
                    Content(
                        item_id=item_id,
                        kind=ContentKind.CHUNK_SUMMARY,
                        text=summary,
                        metadata_json={
                            "stage": "chunk",
                            "chunk_index": index,
                            "chunk_size_chars": self.chunk_size_chars,
                            "overlap_chars": self.overlap_chars,
                            "chunk_sha256": chunk_sha256,
                        },
                    )
                )
                await session.commit()
            summaries.append(summary)
        return summaries

    def _bounded_aggregate(self, summaries: list[str]) -> str:
        """Aggregate summaries while keeping final analyzer input bounded."""
        if not summaries:
            return ""
        separator = "\n\n"
        budget = max(1, self.chunk_size_chars - len(separator) * (len(summaries) - 1))
        per_summary = max(1, budget // len(summaries))
        aggregate = separator.join(summary[:per_summary] for summary in summaries)
        return aggregate[: self.chunk_size_chars]


async def existing_categories(session: AsyncSession, user_id: int) -> list[str]:
    # Категории — динамические строки, отдельная таблица не нужна (DISTINCT достаточно).
    rows = await session.scalars(
        select(Item.category).where(Item.user_id == user_id, Item.category.is_not(None)).distinct()
    )
    return sorted(rows.all())
