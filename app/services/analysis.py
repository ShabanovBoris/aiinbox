import hashlib
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind
from app.domain.language import response_language_for
from app.domain.models import (
    AnalysisResult,
    NormalizedContent,
    TopicClassificationResult,
    UserProfile,
)
from app.domain.signals import (
    has_multiple_sources,
    inconclusive_title_suffix,
    states_inconclusive_comparison,
    title_states_inconclusive,
)
from app.llm.base import LlmError, LlmProvider
from app.llm.untrusted import without_control_instructions
from app.storage.models import Content, Item

# Increment when chunk-summary semantics change so durable evidence is not silently
# reused after a prompt change that asks the model to preserve different information.
CHUNK_SUMMARY_GENERATOR_VERSION = 5


def analysis_text_projection(text: str) -> str:
    """The single sanitized evidence text shared by analysis and evaluation.

    Prompt construction, chunking, and the deterministic inconclusive-title repair
    must all see the same text; otherwise a command removed from the model input
    can still change the generated title, and the evaluation would not reproduce
    production behavior.
    """
    return without_control_instructions(text)


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


_TITLE_LIMIT = 300
# A long title keeps the final clause because it usually carries the verdict and
# the condition that scopes it ("…, but the gain reversed under peak load"), while
# the claim clause keeps its head (subject/verb/negation) even when a short label
# precedes it or the clause itself must shrink.
_TITLE_CLAUSE_BREAK = re.compile(
    r"(?<=[,;:])\s+|\s+(?=\b(?:but|however|yet|while|whereas|но|однако)\b)"
)


def _head_words(text: str, budget: int) -> str:
    """The first words of ``text`` that fit ``budget`` characters."""
    words: list[str] = []
    size = 0
    for word in text.split():
        extra = len(word) + (1 if words else 0)
        if size + extra > budget:
            break
        words.append(word)
        size += extra
    return " ".join(words).rstrip(" ,;:")


_TITLE_NEGATION = re.compile(r"(?i)\b(?:not|never|no|n't|не|ни)\b")


def _trim_title_body(body: str, budget: int) -> str:
    """Keep the claim and the evidence that scopes it inside ``budget``."""
    clauses = [
        clause.strip().rstrip(" ,;:")
        for clause in _TITLE_CLAUSE_BREAK.split(body)
        if clause.strip(" ,;:")
    ]
    if len(clauses) == 1:
        # A single long claim has no later clause to preserve. Cutting its tail
        # keeps the subject, verb, and negation; cutting its head would turn
        # "Preloading did not improve accuracy" into "improve accuracy".
        return _head_words(clauses[0], budget) or body[:budget]
    # A negated claim is the one clause whose head must survive even when it does
    # not fit whole: dropping it changes the result to its opposite.
    negative_index = next(
        (index for index, clause in enumerate(clauses[:-1]) if _TITLE_NEGATION.search(clause)),
        None,
    )
    kept = clauses[-1]
    for index in range(len(clauses) - 2, -1, -1):
        clause = clauses[index]
        candidate = f"{clause}, {kept}"
        if len(candidate) <= budget:
            kept = candidate
            continue
        spare = budget - len(kept) - 2
        if index > 0 and index != negative_index:
            # A middle clause that does not fit is dropped whole; the first clause
            # and a negated claim still get their heads preserved.
            continue
        if spare <= 0:
            break
        partial = _head_words(clause, spare)
        if partial:
            kept = f"{partial}, {kept}"
        if index == 0 or index == negative_index:
            break
    if len(kept) > budget:
        # Never tail-cut a claim: the last words may be the predicate of a longer
        # clause, so a head trim keeps at least the subject and its negation.
        kept = _head_words(kept, budget)
    if not kept:
        # A single unbreakable token still has to fit the schema cap.
        kept = body[:budget]
    return kept


def _fit_title_with_verdict(title: str, suffix: str) -> str:
    """Keep the mandatory verdict and the condition that scopes it inside the cap.

    The verdict is the conclusion the repair exists to preserve, so an over-long
    title drops whole middle clauses and keeps the claim's subject/verb/negation,
    rather than leaving a truncated predicate plus the suffix.
    """
    budget = _TITLE_LIMIT - len(suffix)
    body = title.strip().rstrip(" .;,")
    if budget <= 0:
        return suffix.removeprefix("; ").strip()[:_TITLE_LIMIT]
    if len(body) > budget:
        body = _trim_title_body(body, budget)
    if not body:
        return suffix.removeprefix("; ").strip()[:_TITLE_LIMIT]
    return (body + suffix)[:_TITLE_LIMIT]


def ensure_explicit_inconclusive_title(
    result: AnalysisResult,
    evidence: str,
    *,
    response_language: str | None = None,
    multi_source: bool = False,
) -> AnalysisResult:
    """Carry an explicit no-winner verdict into the title.

    The prompt requires this, but live evaluation showed a stochastic omission that
    silently drops a conclusion the source states. The repair appends only that
    verdict and keeps every measured detail the model wrote; it never invents a
    result or a winner. A title that already states the verdict is unchanged.
    Independent sources get no invented shared verdict, and the suffix follows the
    actual response language (an untranslatable language leaves the title alone
    rather than leaking English into it).
    """
    if multi_source or not states_inconclusive_comparison(evidence):
        return result
    if title_states_inconclusive(result.title):
        return result
    suffix = inconclusive_title_suffix(result.title, response_language, evidence)
    if not suffix:
        return result
    title = _fit_title_with_verdict(result.title, suffix)
    return result.model_copy(update={"title": title})


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
        result = await self.provider.analyze(content, profile, categories)
        return ensure_explicit_inconclusive_title(
            result,
            content.text,
            response_language=response_language_for(content, profile),
            multi_source=has_multiple_sources(content.metadata),
        )

    async def prepare_content(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        item_id: int | None = None,
    ) -> NormalizedContent:
        """Prepare one bounded, durable evidence projection shared by both LLM stages."""
        # Strip output-control commands before chunking: a checkpoint generated
        # from an injected chunk would otherwise preserve the distortion, and a
        # final aggregate filter cannot recover evidence that was never summarized.
        prepared_text = analysis_text_projection(content.text)
        # The model, deterministic repair, chunking, and the opt-in evaluation all
        # read this one sanitized projection. Returning the raw short text here let
        # a removed command still drive the repair decision.
        content = content.model_copy(update={"text": prepared_text})
        if len(prepared_text) > self.chunk_size_chars:
            if item_id is None:
                raise ValueError("item_id is required for durable long-content analysis")
            chunks = split_text(prepared_text, self.chunk_size_chars, self.overlap_chars)
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
    r"winner|mixed|undetermined|undecided|no verdict|"
    r"итог\w*|результат\w*|вывод\w*|выясн\w*|показа\w*|сниз\w*|увелич\w*|улучш\w*|"
    r"хуже|лучше|не удалось|однако|только|кроме|огранич\w*|обратн\w*|"
    r"победител\w*|неопределен\w*|смешанн\w*|противореч\w*)\b",
    re.IGNORECASE,
)


# A late practical answer ("Use lifecycle-aware collection to prevent duplicate
# requests.") has no measurement cue, but it is still the conclusion the summary
# must keep. This only ranks retention; it never rewrites the sentence.
_FINAL_ANSWER_CUE = re.compile(
    r"(?i)^(?:use|avoid|prefer|choose|adopt|apply|enable|disable|keep|switch|"
    r"migrate|replace|store|cache|call|wrap|collect|implement|configure|set|"
    r"do\s+not|don't|never)\b"
    r"|\b(?:should|recommend\w*|best\s+practice|to\s+prevent|to\s+avoid|"
    r"to\s+reduce|чтобы\s+избежать|следует|рекоменд\w*|используй\w*|избегай\w*)\b"
)


def _sentences(text: str) -> list[str]:
    """Split compact summary prose into sentences without losing trailing content."""
    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(text.strip()) if part.strip()]
    return parts or [text.strip()]


def _sentence_priority(sentence: str, index: int, count: int) -> int:
    score = 4 if index == 0 else 0
    cued = _EVIDENCE_CUE.search(sentence) is not None
    if index == count - 1 and cued:
        # A final verdict or recap earns its position only when it carries
        # evidence; a cue-less closing sentence must not displace a measured result.
        score += 4
    if cued:
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


def _answer_sentence_index(sentences: list[str]) -> int | None:
    """The latest sentence that carries a result, verdict, limitation, or recommendation.

    A cue-less closing recap is not an answer: on a small budget it must not displace
    the measured result that precedes it.
    """
    count = len(sentences)
    if count <= 1:
        return None
    if _EVIDENCE_CUE.search(sentences[-1]) or _FINAL_ANSWER_CUE.search(sentences[-1]):
        return count - 1
    for index in range(count - 2, 0, -1):
        if _EVIDENCE_CUE.search(sentences[index]):
            return index
    return None


def _select_sentences(sentences: list[str], limit: int) -> str | None:
    """Keep complete sentences, preferring cued outcomes and the latest caveat."""
    count = len(sentences)
    answer_index = _answer_sentence_index(sentences)
    # The first sentence carries the question/goal; the answer carries the result.
    # When both cannot fit, the answer wins: a question with no conclusion is worse
    # than a conclusion without its setup.
    mandatory = {0}
    if answer_index is not None and answer_index != 0:
        mandatory.add(answer_index)
    used = len(_render_sentences(sentences, mandatory))
    if used > limit:
        if answer_index is None or answer_index == 0:
            return None
        mandatory = {answer_index}
        used = len(_render_sentences(sentences, mandatory))
        if used > limit:
            return None

    priorities = {
        index: _sentence_priority(sentences[index], index, count) for index in range(count)
    }
    chosen = set(mandatory)
    # Equal cues: keep the later sentence. A late conclusion must outrank earlier
    # setup when a compressed checkpoint cannot retain everything.
    ordered_candidates = sorted(
        (index for index in range(count) if index not in chosen),
        key=lambda index: (priorities[index], index),
        reverse=True,
    )
    for index in ordered_candidates:
        # +1 for the joining space and marker space keeps the bound conservative.
        if used + len(sentences[index]) + 1 + len(_OMISSION_MARKER) <= limit:
            chosen.add(index)
            used += len(sentences[index]) + 1

    rendered = _render_sentences(sentences, chosen)
    # The greedy estimate can be off by a marker or two; drop the weakest optional
    # sentence until the exact rendered text fits. Every mandatory sentence stays.
    while len(rendered) > limit and chosen - mandatory:
        weakest = min(chosen - mandatory, key=lambda index: (priorities[index], index))
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
