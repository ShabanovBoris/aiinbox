import hashlib
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind
from app.domain.models import (
    AnalysisResult,
    NormalizedContent,
    TopicClassificationResult,
    UserProfile,
)
from app.llm.base import LlmError, LlmProvider
from app.storage.models import Content, Item

# Increment when chunk-summary semantics change so durable evidence is not silently
# reused after a prompt change that asks the model to preserve different information.
CHUNK_SUMMARY_GENERATOR_VERSION = 3


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
    """Own bounded analysis and isolated topic-classification provider boundaries.

    Profile relevance and canonical topic evidence stay separate so either LLM
    stage can resume without repeating the other.
    """

    def __init__(
        self, provider: LlmProvider, chunk_size_chars: int = 12_000, overlap_chars: int = 0
    ):
        self.provider = provider
        # Chunking belongs to the application boundary; both final LLM stages
        # receive the same persisted, bounded evidence projection.
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
        content = await self.prepare_content(content, session, item_id)
        categories = await existing_categories(session, user_id)
        # The provider call can take seconds; finish the read transaction first so
        # another worker can claim/update its SQLite outbox rows while analysis runs.
        await session.commit()
        return await self.provider.analyze(content, profile, categories)

    async def prepare_content(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        item_id: int | None = None,
    ) -> NormalizedContent:
        """Prepare one bounded, durable evidence projection shared by both LLM stages."""
        if len(content.text) > self.chunk_size_chars:
            if item_id is None:
                raise ValueError("item_id is required for durable long-content analysis")
            chunks = split_text(content.text, self.chunk_size_chars, self.overlap_chars)
            summaries = await self._durable_chunk_summaries(session, item_id, chunks)
            content = content.model_copy(
                update={
                    "text": bounded_chunk_aggregate(
                        summaries, chunk_size_chars=self.chunk_size_chars
                    ),
                    "metadata": {
                        **content.metadata,
                        "_analysis_chunk_summary_count": len(summaries),
                    },
                }
            )
        return content

    @staticmethod
    def topic_classification_content(
        content: NormalizedContent,
    ) -> NormalizedContent | None:
        """Keep user intent out of the canonical category evidence boundary."""
        metadata = {
            key: content.metadata[key]
            for key in ("visual_notes", "source_count", "successful_source_count")
            if key in content.metadata
        }
        source_count = content.metadata.get("source_count")
        successful_source_count = content.metadata.get("successful_source_count")
        if (
            type(source_count) is int
            and source_count > 0
            and type(successful_source_count) is int
            and successful_source_count == 0
        ):
            if content.source_context and content.source_context.strip():
                return content.model_copy(
                    update={"text": "", "user_note": None, "metadata": metadata}
                )
            return None
        return content.model_copy(update={"user_note": None, "metadata": metadata})

    async def classify_topic(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        user_id: int,
    ) -> TopicClassificationResult | None:
        """Run the independent category stage after its primary analysis checkpoint."""
        topic_content = self.topic_classification_content(content)
        if topic_content is None:
            return None
        categories = await existing_categories(session, user_id)
        # End the category read transaction before the independent provider call.
        await session.commit()
        return await self.provider.classify_topic(topic_content, categories)

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


_OMISSION_MARKER = " … "
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
# A checkpoint summary is already compressed prose. When the aggregate budget is
# smaller than the checkpoint, drop whole filler sentences instead of cutting a
# result sentence in half. These cues only rank sentences for retention; they never
# create or rewrite evidence.
_EVIDENCE_CUE = re.compile(
    r"\d|\b(?:result|finding|found|shown?|measured|increas\w*|decreas\w*|reduc\w*|"
    r"improv\w*|worse|better|no conclusion|inconclusive|unresolved|caveat|however|"
    r"but|only|except|limit\w*|revers\w*|fail\w*|final|answer|conclusion|"
    r"итог\w*|результат\w*|вывод\w*|выясн\w*|показа\w*|сниз\w*|увелич\w*|улучш\w*|"
    r"хуже|лучше|не удалось|однако|только|кроме|огранич\w*|обратн\w*)\b",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    """Split compact summary prose into sentences without losing trailing content."""
    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(text.strip()) if part.strip()]
    return parts or [text.strip()]


def _sentence_priority(sentence: str, index: int, count: int) -> int:
    score = 0
    if index == 0 or index == count - 1:
        score += 4
    if _EVIDENCE_CUE.search(sentence):
        score += 3
    return score


def _render_sentences(sentences: list[str], chosen: set[int]) -> str:
    """Join selected sentences in source order and mark every omitted gap."""
    pieces: list[str] = []
    skipped = False
    for index, sentence in enumerate(sentences):
        if index in chosen:
            if skipped and pieces:
                pieces.append(_OMISSION_MARKER.strip())
                skipped = False
            pieces.append(sentence)
        else:
            skipped = True
    return " ".join(pieces)


def _select_sentences(sentences: list[str], limit: int) -> str | None:
    """Keep complete sentences, preferring edges and explicitly cued outcomes."""
    count = len(sentences)
    # The first sentence carries the question/goal and the last carries the latest
    # outcome or caveat, so both are mandatory unless the budget cannot fit them.
    mandatory = {0, count - 1}
    used = sum(len(sentences[index]) for index in mandatory)
    if used + len(_OMISSION_MARKER) * (count - 1) > limit:
        return None

    priorities = {
        index: _sentence_priority(sentences[index], index, count) for index in range(count)
    }
    chosen = set(mandatory)
    ordered_candidates = sorted(
        (index for index in range(count) if index not in chosen),
        key=lambda index: (priorities[index], -index),
        reverse=True,
    )
    for index in ordered_candidates:
        # +1 for the joining space and marker space keeps the bound conservative.
        if used + len(sentences[index]) + 1 + len(_OMISSION_MARKER) <= limit:
            chosen.add(index)
            used += len(sentences[index]) + 1

    rendered = _render_sentences(sentences, chosen)
    # The greedy estimate can be off by a marker or two; drop the weakest optional
    # sentence until the exact rendered text fits. Mandatory edges stay in place.
    while len(rendered) > limit and chosen - mandatory:
        weakest = min(chosen - mandatory, key=lambda index: (priorities[index], -index))
        chosen.remove(weakest)
        rendered = _render_sentences(sentences, chosen)
    return rendered if len(rendered) <= limit else None


def _bounded_summary(text: str, limit: int) -> str:
    """Keep the evidence that must survive when one checkpoint has to shrink.

    Character slicing silently drops a conclusion or caveat that sits inside a
    long checkpoint. Sentence-aware selection keeps the opening and the latest
    outcome plus cued result/limitation sentences, and marks every omitted gap
    instead of pretending the summary was complete.
    """
    if len(text) <= limit:
        return text
    if limit <= len(_OMISSION_MARKER) + 2:
        return text[:limit]
    selected = _select_sentences(_sentences(text), limit)
    if selected is not None:
        return selected
    body = limit - len(_OMISSION_MARKER)
    head = (body + 1) // 2
    tail = body - head
    return text[:head].rstrip() + _OMISSION_MARKER + text[-tail:].lstrip()


def bounded_chunk_aggregate(summaries: list[str], *, chunk_size_chars: int) -> str:
    """Aggregate ordered checkpoints within one bounded analyzer input.

    Shared with the opt-in quality evaluation so manual review exercises exactly
    the production aggregation instead of a reimplementation.
    """
    if not summaries:
        return ""
    separator = "\n\n"
    labels = [f"CHUNK {index + 1}/{len(summaries)}:\n" for index in range(len(summaries))]
    label_budget = sum(map(len, labels)) + len(separator) * (len(summaries) - 1)
    summary_budget = chunk_size_chars - label_budget
    if summary_budget < len(summaries):
        # Small test/config budgets cannot fit useful labels; retain equal-share
        # truncation so each chunk still contributes when the bound permits.
        budget = max(1, chunk_size_chars - len(separator) * (len(summaries) - 1))
        per_summary = max(1, budget // len(summaries))
        aggregate = separator.join(_bounded_summary(summary, per_summary) for summary in summaries)
        return aggregate[:chunk_size_chars]

    per_summary = summary_budget // len(summaries)
    # These order labels are prompt framing only; durable rows keep plain
    # summaries so search/export never expose analyzer bookkeeping.
    return separator.join(
        f"{label}{_bounded_summary(summary, per_summary)}"
        for label, summary in zip(labels, summaries)
    )


async def existing_categories(session: AsyncSession, user_id: int) -> list[str]:
    # Категории — динамические строки, отдельная таблица не нужна (DISTINCT достаточно).
    rows = await session.scalars(
        select(Item.category).where(Item.user_id == user_id, Item.category.is_not(None)).distinct()
    )
    return sorted(rows.all())
