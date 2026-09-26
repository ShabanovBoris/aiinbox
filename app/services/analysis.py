import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind
from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.llm.base import LlmError, LlmProvider
from app.storage.models import Content, Item

# Increment when chunk-summary semantics change so durable evidence is not silently
# reused after a prompt change that asks the model to preserve different information.
CHUNK_SUMMARY_GENERATOR_VERSION = 2


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
            boundary_from = start + overlap_chars
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
            content = content.model_copy(
                update={
                    "text": self._bounded_aggregate(summaries),
                    "metadata": {
                        **content.metadata,
                        "_analysis_chunk_summary_count": len(summaries),
                    },
                }
            )
        categories = await existing_categories(session, user_id)
        # The provider call can take seconds; finish the read transaction first so
        # another worker can claim/update its SQLite outbox rows while analysis runs.
        await session.commit()
        analysis = await self.provider.analyze(content, profile, categories)
        # The topic boundary receives captured source data only. User intent remains
        # available to relevance analysis but cannot influence canonical category.
        topic_content = content.model_copy(update={"user_note": None})
        topic = await self.provider.classify_topic(topic_content, categories)
        return analysis.model_copy(update={"category": topic.category})

    async def _durable_chunk_summaries(
        self, session: AsyncSession, item_id: int, chunks: list[str]
    ) -> list[str]:
        """Persist each expensive summary before requesting final analysis."""
        rows = (
            await session.scalars(
                select(Content)
                .where(
                    Content.item_id == item_id,
                    Content.kind == ContentKind.CHUNK_SUMMARY,
                )
                .order_by(Content.id)
            )
        ).all()
        # Close the lookup transaction before any provider call; each completed
        # replacement is then committed as its own restart-safe checkpoint.
        await session.commit()

        rows_by_index: dict[int, list[Content]] = {}
        for row in rows:
            metadata = row.metadata_json or {}
            chunk_index = metadata.get("chunk_index")
            if metadata.get("stage") == "chunk" and type(chunk_index) is int and chunk_index >= 0:
                rows_by_index.setdefault(chunk_index, []).append(row)

        summaries = []
        used_row_ids = set()
        for index, chunk in enumerate(chunks):
            chunk_sha256 = hashlib.sha256(chunk.encode()).hexdigest()
            candidates = rows_by_index.get(index, [])
            row = next(
                (
                    candidate
                    for candidate in reversed(candidates)
                    if (metadata := candidate.metadata_json or {}).get("stage") == "chunk"
                    and metadata.get("chunk_index") == index
                    and metadata.get("chunk_size_chars") == self.chunk_size_chars
                    and metadata.get("overlap_chars") == self.overlap_chars
                    and metadata.get("chunk_sha256") == chunk_sha256
                    and metadata.get("generator_version") == CHUNK_SUMMARY_GENERATOR_VERSION
                    and candidate.text.strip()
                ),
                None,
            )
            if row is None:
                summary = (await self.provider.summarize_chunk(chunk)).strip()
                if not summary:
                    raise LlmError("INVALID_LLM_OUTPUT", "empty chunk summary")
                summary = summary[: self.chunk_size_chars]
                metadata = {
                    "stage": "chunk",
                    "chunk_index": index,
                    "chunk_size_chars": self.chunk_size_chars,
                    "overlap_chars": self.overlap_chars,
                    "chunk_sha256": chunk_sha256,
                    "generator_version": CHUNK_SUMMARY_GENERATOR_VERSION,
                }
                if candidates:
                    # Replacing the derived row keeps old prompt generations out
                    # of FTS/Ask instead of accumulating duplicate summaries.
                    row = candidates[-1]
                    row.text = summary
                    row.metadata_json = metadata
                else:
                    row = Content(
                        item_id=item_id,
                        kind=ContentKind.CHUNK_SUMMARY,
                        text=summary,
                        metadata_json=metadata,
                    )
                    session.add(row)
                    rows_by_index.setdefault(index, []).append(row)
                await session.commit()
            used_row_ids.add(row.id)
            summaries.append(row.text)

        stale_rows = [row for row in rows if row.id not in used_row_ids]
        if stale_rows:
            # ❌ Удалены устаревшие CHUNK_SUMMARY: FTS и Ask fallback должны видеть
            # только текущие checkpoints, а не старые версии или прежнюю разбивку.
            for row in stale_rows:
                await session.delete(row)
            await session.commit()
        return summaries

    def _bounded_aggregate(self, summaries: list[str]) -> str:
        """Aggregate summaries while keeping final analyzer input bounded."""
        if not summaries:
            return ""
        separator = "\n\n"
        labels = [f"CHUNK {index + 1}/{len(summaries)}:\n" for index in range(len(summaries))]
        label_budget = sum(map(len, labels)) + len(separator) * (len(summaries) - 1)
        summary_budget = self.chunk_size_chars - label_budget
        if summary_budget < len(summaries):
            # Small test/config budgets cannot fit useful labels; retain equal-share
            # truncation so each chunk still contributes when the bound permits.
            budget = max(1, self.chunk_size_chars - len(separator) * (len(summaries) - 1))
            per_summary = max(1, budget // len(summaries))
            aggregate = separator.join(summary[:per_summary] for summary in summaries)
            return aggregate[: self.chunk_size_chars]

        per_summary = summary_budget // len(summaries)
        # These order labels are prompt framing only; durable rows keep plain
        # summaries so search/export never expose analyzer bookkeeping.
        return separator.join(
            f"{label}{summary[:per_summary]}" for label, summary in zip(labels, summaries)
        )


async def existing_categories(session: AsyncSession, user_id: int) -> list[str]:
    # Категории — динамические строки, отдельная таблица не нужна (DISTINCT достаточно).
    rows = await session.scalars(
        select(Item.category).where(Item.user_id == user_id, Item.category.is_not(None)).distinct()
    )
    return sorted(rows.all())
