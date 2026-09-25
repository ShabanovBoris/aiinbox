"""Bounded, user-scoped retrieval and synthesis over persisted Inbox evidence."""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.enums import ContentKind
from app.domain.models import (
    AskDeliveryPayload,
    AskInboxCitation,
    AskInboxResult,
    AskReference,
)
from app.errors import AppError
from app.llm.base import LlmProvider
from app.services.retrieval import SearchHit, search_item_hits
from app.storage.models import AskJob, Content, Item, ItemSource

log = logging.getLogger(__name__)

MAX_ASK_QUESTION_CHARS = 2000
DEFAULT_ASK_RETRIEVAL_LIMIT = 8
MAX_ASK_RETRIEVAL_LIMIT = 10
MAX_ASK_ITEM_CONTEXT_CHARS = 6000
MAX_ASK_TOTAL_CONTEXT_CHARS = 40_000
ASK_EXCERPT_CHARS = 1400
_MAX_EXCERPTS_PER_CONTENT = 4
_MAX_USER_NOTE_CONTEXT_CHARS = 800
_MAX_REFERENCE_TITLE_CHARS = 120
_NO_RESULTS_RU = "Я не нашёл в сохранённых материалах достаточно данных по этому вопросу."
_NO_RESULTS_EN = "I couldn't find enough information for this question in your saved materials."
_INSUFFICIENT_RU = "В найденных материалах недостаточно данных для уверенного ответа."
_INSUFFICIENT_EN = "The saved materials do not contain enough information to answer confidently."
_FAILED_RU = "Не удалось подготовить ответ по сохранённым материалам. Попробуй ещё раз."
_FAILED_EN = "I couldn't prepare an answer from your saved materials. Please try again."
_PRIMARY_KINDS = {
    ContentKind.USER_TEXT,
    ContentKind.WEB_TEXT,
    ContentKind.DOCUMENT_TEXT,
    ContentKind.TRANSCRIPT,
    ContentKind.VISUAL_NOTES,
    ContentKind.DESCRIPTION,
}


@dataclass(frozen=True, slots=True)
class _Excerpt:
    """Candidate evidence unit retained only while applying context fairness budgets."""

    content_id: int
    source_id: int | None
    source_type: str | None
    source_url: str | None
    kind: str
    text: str
    score: int
    start: int


@dataclass(frozen=True, slots=True)
class AskContext:
    """Immutable provider input plus the exact citation namespace it received."""

    text: str
    references: tuple[AskReference, ...]
    item_ids: tuple[int, ...]

    @property
    def reference_by_key(self) -> dict[tuple[int, int | None], AskReference]:
        return {(ref.item_id, ref.source_id): ref for ref in self.references}


def _json_field(name: str, value: str) -> str:
    """Quote persisted strings so multiline source text cannot impersonate provenance labels."""
    return f"{name}: {json.dumps(value, ensure_ascii=False)}"


def safe_http_url(value: str | None) -> str | None:
    """Apply the Ask presentation boundary before an untrusted saved URL enters a prompt/button."""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _query_terms(question: str) -> frozenset[str]:
    """Use Unicode word tokens like FTS5's tokenizer when locating local excerpts."""
    return frozenset(match.group().casefold() for match in re.finditer(r"[^\W_]+", question))


def _excerpt_windows(text: str, terms: frozenset[str]) -> list[tuple[int, int, str, int]]:
    """Select a few non-overlapping bounded windows near lexical question matches."""
    if not text.strip():
        return []
    tokens = list(re.finditer(r"[^\W_]+", text))
    matches = [token for token in tokens if token.group().casefold() in terms]
    if len(text) <= ASK_EXCERPT_CHARS:
        score = sum(token.group().casefold() in terms for token in tokens)
        return [(0, len(text), text, score)]
    if not matches:
        end = min(len(text), ASK_EXCERPT_CHARS)
        return [(0, end, text[:end], 0)]

    # Coalesce frequent lexical matches into half-window buckets first. This
    # bounds excerpt scoring for long transcripts with common question words.
    buckets: dict[int, tuple[int, int]] = {}
    bucket_width = ASK_EXCERPT_CHARS // 2
    for match in matches:
        bucket = match.start() // bucket_width
        first_position, score = buckets.get(bucket, (match.start(), 0))
        buckets[bucket] = (first_position, score + 1)

    windows: list[tuple[int, int, str, int]] = []
    for position, match_score in buckets.values():
        start = max(0, position - ASK_EXCERPT_CHARS // 2)
        end = min(len(text), start + ASK_EXCERPT_CHARS)
        if end - start < ASK_EXCERPT_CHARS:
            start = max(0, end - ASK_EXCERPT_CHARS)
        if start:
            boundary = text.find(" ", start, min(start + 120, len(text)))
            if boundary >= 0:
                start = boundary + 1
                end = min(len(text), start + ASK_EXCERPT_CHARS)
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        excerpt = text[start:end].strip()
        if not excerpt:
            continue
        windows.append((start, end, excerpt, match_score))

    selected: list[tuple[int, int, str, int]] = []
    for candidate in sorted(windows, key=lambda window: (-window[3], window[0])):
        start, end, _, _ = candidate
        if any(
            start < selected_end and end > selected_start
            for selected_start, selected_end, _, _ in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) == _MAX_EXCERPTS_PER_CONTENT:
            break
    return sorted(selected, key=lambda window: window[0])


def _item_header(item: Item, item_url: str | None) -> str:
    """Render Item metadata separately from JSON-quoted untrusted source evidence."""
    item_type = item.item_type.value if item.item_type is not None else ""
    lines = [
        f"ITEM_ID: {item.id}",
        _json_field("TITLE_JSON", (item.title or "(untitled)")[:300]),
        _json_field("SUMMARY_JSON", (item.summary or "")[:1200]),
        _json_field("CATEGORY_JSON", (item.category or "")[:100]),
        f"ITEM_TYPE: {item_type}",
        f"CREATED_AT: {item.created_at.isoformat() if item.created_at else ''}",
    ]
    if item.user_note:
        lines.append(_json_field("USER_NOTE_JSON", item.user_note[:_MAX_USER_NOTE_CONTEXT_CHARS]))
    if item_url:
        lines.append(_json_field("ITEM_SOURCE_URL_JSON", item_url))
    return "\n".join(lines)


def _evidence_block(excerpt: _Excerpt) -> str:
    """Keep each bounded excerpt attached to its persisted Item/source identity."""
    source_id = str(excerpt.source_id) if excerpt.source_id is not None else "ITEM_LEVEL"
    lines = [
        f"SOURCE_ID: {source_id}",
        f"SOURCE_TYPE: {excerpt.source_type or 'ITEM_LEVEL'}",
    ]
    if excerpt.source_url:
        lines.append(_json_field("SOURCE_URL_JSON", excerpt.source_url))
    lines.extend((f"CONTENT_KIND: {excerpt.kind}", _json_field("EXCERPT_JSON", excerpt.text)))
    return "\n".join(lines)


def unique_item_source_url(item: Item, child_sources: list[ItemSource]) -> str | None:
    """Permit Item-level open buttons only when persisted source identity is unambiguous."""
    urls = {
        url
        for url in [safe_http_url(item.source_url)]
        + [safe_http_url(source.source_url) for source in child_sources]
        if url
    }
    return next(iter(urls)) if len(urls) == 1 else None


def build_ask_context(
    question: str,
    hits: list[SearchHit],
    items: list[Item],
    sources: list[ItemSource],
    contents: list[Content],
    *,
    ask_job_id: int | None = None,
) -> AskContext:
    """Project top FTS hits into fair, bounded evidence without leaking ORM rows."""
    item_by_id = {item.id: item for item in items}
    hit_items = [item_by_id[hit.item_id] for hit in hits if hit.item_id in item_by_id]
    if not hit_items:
        return AskContext(text="", references=(), item_ids=())

    sources_by_item: dict[int, list[ItemSource]] = {item.id: [] for item in hit_items}
    source_by_id: dict[int, ItemSource] = {}
    for source in sources:
        if source.item_id in sources_by_item:
            sources_by_item[source.item_id].append(source)
            source_by_id[source.id] = source
    for item_sources in sources_by_item.values():
        item_sources.sort(key=lambda source: (source.source_index, source.id))

    contents_by_item: dict[int, list[Content]] = {item.id: [] for item in hit_items}
    for content in contents:
        if content.item_id in contents_by_item:
            contents_by_item[content.item_id].append(content)
    for item_contents in contents_by_item.values():
        item_contents.sort(key=lambda content: content.id)

    terms = _query_terms(question)
    item_blocks: list[tuple[int, str, set[int], AskReference]] = []
    for item in hit_items:
        item_sources = sources_by_item[item.id]
        valid_rows: list[Content] = []
        for row in contents_by_item[item.id]:
            if row.source_id is not None:
                source = source_by_id.get(row.source_id)
                if source is None or source.item_id != row.item_id:
                    log.warning(
                        "ask skipped mismatched content provenance job=%s item_id=%s source_id=%s",
                        ask_job_id,
                        row.item_id,
                        row.source_id,
                    )
                    continue
            if row.kind in _PRIMARY_KINDS and row.text.strip():
                valid_rows.append(row)

        # Chunk summaries are aggregate analysis checkpoints, so they may stand in
        # only when no original persisted evidence remains usable for this Item.
        fallback = not valid_rows
        evidence_rows = valid_rows or [
            row
            for row in contents_by_item[item.id]
            if row.kind == ContentKind.CHUNK_SUMMARY and row.text.strip()
        ]
        grouped: dict[int | None, list[_Excerpt]] = {}
        for row in evidence_rows:
            source = source_by_id.get(row.source_id) if row.source_id is not None else None
            if row.source_id is not None and source is None:
                continue
            effective_source_id = None if fallback else row.source_id
            source_type = source.source_type.value if source and not fallback else None
            source_url = safe_http_url(source.source_url) if source and not fallback else None
            for start, _end, excerpt, score in _excerpt_windows(row.text, terms):
                grouped.setdefault(effective_source_id, []).append(
                    _Excerpt(
                        content_id=row.id,
                        source_id=effective_source_id,
                        source_type=source_type,
                        source_url=source_url,
                        kind=row.kind.value,
                        text=excerpt,
                        score=score,
                        start=start,
                    )
                )

        ordered_group_ids = [None] if None in grouped else []
        ordered_group_ids.extend(source.id for source in item_sources if source.id in grouped)
        group_candidates = [
            sorted(grouped[group_id], key=lambda row: (-row.score, row.content_id, row.start))
            for group_id in ordered_group_ids
        ]
        item_url = unique_item_source_url(item, item_sources)
        header = _item_header(item, item_url)
        selected: list[_Excerpt] = []
        offsets = [0] * len(group_candidates)
        item_budget = min(
            MAX_ASK_ITEM_CONTEXT_CHARS,
            (MAX_ASK_TOTAL_CONTEXT_CHARS - 2 * (len(hit_items) - 1)) // len(hit_items),
        )

        # The first pass gives each composite source a chance before any source
        # receives a second excerpt; later passes rotate in stable source order.
        for group_index, candidates in enumerate(group_candidates):
            if candidates:
                candidate = candidates[0]
                proposed = "\n\n".join(
                    [
                        header,
                        *(_evidence_block(value) for value in selected),
                        _evidence_block(candidate),
                    ]
                )
                if len(proposed) <= item_budget:
                    selected.append(candidate)
                    offsets[group_index] = 1
        while True:
            added = False
            for group_index, candidates in enumerate(group_candidates):
                offset = offsets[group_index]
                if offset >= len(candidates):
                    continue
                candidate = candidates[offset]
                proposed = "\n\n".join(
                    [
                        header,
                        *(_evidence_block(value) for value in selected),
                        _evidence_block(candidate),
                    ]
                )
                offsets[group_index] += 1
                if len(proposed) <= item_budget:
                    selected.append(candidate)
                    added = True
            if not added:
                break

        rendered = "\n\n".join([header, *(_evidence_block(value) for value in selected)])
        included_source_ids = {row.source_id for row in selected if row.source_id is not None}
        item_reference = AskReference(
            item_id=item.id,
            source_id=None,
            title=(item.title or "(untitled)")[:_MAX_REFERENCE_TITLE_CHARS],
            source_type=None,
            source_url=item_url,
        )
        item_blocks.append((item.id, rendered, included_source_ids, item_reference))

    blocks: list[str] = []
    references: list[AskReference] = []
    item_ids: list[int] = []
    for item_id, block, included_source_ids, item_reference in item_blocks:
        separator = "\n\n" if blocks else ""
        if sum(map(len, blocks)) + len(separator) + len(block) > MAX_ASK_TOTAL_CONTEXT_CHARS:
            break
        blocks.append(block)
        item_ids.append(item_id)
        references.append(item_reference)
        for source in sources_by_item[item_id]:
            if source.id not in included_source_ids:
                continue
            source_id = source.id
            source = source_by_id.get(source_id)
            if source is None or source.item_id != item_id:
                continue
            item = item_by_id[item_id]
            references.append(
                AskReference(
                    item_id=item_id,
                    source_id=source_id,
                    title=(item.title or "(untitled)")[:_MAX_REFERENCE_TITLE_CHARS],
                    source_type=source.source_type.value,
                    source_url=safe_http_url(source.source_url),
                )
            )
    context = "\n\n".join(blocks)
    return AskContext(text=context, references=tuple(references), item_ids=tuple(item_ids))


async def enqueue_ask(
    session_factory: async_sessionmaker,
    *,
    telegram_user_id: int,
    telegram_message_id: int | None,
    chat_id: int,
    question: str,
    default_timezone: str = "UTC",
) -> AskJob:
    """Persist the request at the Telegram edge; a DB uniqueness key absorbs redelivery."""
    from app.services.ingestion import get_or_create_user

    question = question.strip()
    if not question:
        raise ValueError("Ask question must not be empty")
    if len(question) > MAX_ASK_QUESTION_CHARS:
        raise ValueError("Ask question exceeds the application length limit")

    async with session_factory() as session:
        user = await get_or_create_user(
            session,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=default_timezone,
        )
        values = {
            "user_id": user.id,
            "telegram_message_id": telegram_message_id,
            "question": question,
            "status": "PENDING",
        }
        if telegram_message_id is None:
            job = AskJob(**values)
            session.add(job)
            await session.flush()
        else:
            result = await session.execute(
                sqlite_insert(AskJob)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[AskJob.user_id, AskJob.telegram_message_id])
                .returning(AskJob.id)
            )
            job_id = result.scalar_one_or_none()
            if job_id is None:
                job = await session.scalar(
                    select(AskJob).where(
                        AskJob.user_id == user.id,
                        AskJob.telegram_message_id == telegram_message_id,
                    )
                )
            else:
                job = await session.get(AskJob, job_id)
        await session.commit()
        log.info("ask job queued id=%s user_id=%s", job.id, user.id)
        return job


async def claim_oldest_ask_job(session_factory: async_sessionmaker) -> AskJob | None:
    """Atomically reserve the oldest request so concurrent workers cannot duplicate synthesis."""
    async with session_factory() as session:
        result = await session.execute(
            update(AskJob)
            .where(
                AskJob.id
                == select(AskJob.id)
                .where(AskJob.status == "PENDING")
                .order_by(AskJob.created_at, AskJob.id)
                .limit(1)
                .scalar_subquery(),
                AskJob.status == "PENDING",
            )
            .values(status="RUNNING", updated_at=func.now())
            .returning(AskJob.id)
        )
        job_id = result.scalar_one_or_none()
        await session.commit()
        return await session.get(AskJob, job_id) if job_id is not None else None


async def requeue_running_ask_jobs(session_factory: async_sessionmaker) -> int:
    """Restore interrupted durable requests to the claimable queue during startup."""
    async with session_factory() as session:
        result = await session.execute(
            update(AskJob).where(AskJob.status == "RUNNING").values(status="PENDING")
        )
        await session.commit()
        if result.rowcount:
            log.warning("requeued RUNNING ask jobs count=%s", result.rowcount)
        return result.rowcount


def _controlled_answer(code: str, language: str) -> AskDeliveryPayload:
    """Keep empty/insufficient/error outcomes application-owned and free of fake citations."""
    is_russian = language.casefold().startswith("ru")
    if code == "NO_RESULTS":
        message = _NO_RESULTS_RU if is_russian else _NO_RESULTS_EN
    elif code == "INSUFFICIENT_CONTEXT":
        message = _INSUFFICIENT_RU if is_russian else _INSUFFICIENT_EN
    else:
        message = _FAILED_RU if is_russian else _FAILED_EN
    return AskDeliveryPayload(answer=message, references=[])


def _validated_answer(
    result: AskInboxResult, context: AskContext
) -> tuple[AskDeliveryPayload | None, str | None]:
    """Accept only citations from the exact immutable context sent to the provider."""
    if result.insufficient_context:
        return None, "insufficient"
    answer = result.answer.strip()
    if not answer:
        return None, "empty_answer"
    allowlist = context.reference_by_key
    references: list[AskInboxCitation] = []
    seen: set[tuple[int, int | None]] = set()
    for citation in result.citations:
        key = (citation.item_id, citation.source_id)
        if key not in allowlist:
            return None, "unknown_citation"
        if key not in seen:
            references.append(citation)
            seen.add(key)
    if not references:
        return None, "uncited_answer"
    return AskDeliveryPayload(answer=answer, references=references), None


class AskInboxService:
    """Own retrieval, evidence projection, language, citation checks, and outbox handoff."""

    def __init__(self, session_factory: async_sessionmaker, provider: LlmProvider):
        self.session_factory = session_factory
        self.provider = provider

    async def process(self, ask_job_id: int) -> None:
        """Build a detached context, close SQLite, synthesize, then atomically enqueue delivery."""
        from app.services.profile import get_profile

        async with self.session_factory() as session:
            job = await session.get(AskJob, ask_job_id)
            if job is None or job.status != "RUNNING":
                return
            user_id = job.user_id
            question = job.question
            hits = await search_item_hits(
                session, user_id, question, limit=DEFAULT_ASK_RETRIEVAL_LIMIT
            )
            if hits:
                item_ids = [hit.item_id for hit in hits]
                items = list(
                    (
                        await session.scalars(
                            select(Item).where(Item.user_id == user_id, Item.id.in_(item_ids))
                        )
                    ).all()
                )
                if items:
                    sources = list(
                        (
                            await session.scalars(
                                select(ItemSource)
                                .where(ItemSource.item_id.in_([item.id for item in items]))
                                .order_by(ItemSource.source_index, ItemSource.id)
                            )
                        ).all()
                    )
                    contents = list(
                        (
                            await session.scalars(
                                select(Content)
                                .where(Content.item_id.in_([item.id for item in items]))
                                .order_by(Content.item_id, Content.id)
                            )
                        ).all()
                    )
                    context = build_ask_context(
                        question, hits, items, sources, contents, ask_job_id=ask_job_id
                    )
                else:
                    context = AskContext(text="", references=(), item_ids=())
            else:
                context = AskContext(text="", references=(), item_ids=())
            profile = await get_profile(session, user_id)
            preferred_language = profile.preferred_language

        # No session or transaction crosses either provider call. Citation retry
        # reuses the exact same bounded evidence and cannot expand its ID allowlist.
        if not context.item_ids:
            payload = _controlled_answer("NO_RESULTS", preferred_language)
            await self._complete(ask_job_id, user_id, payload)
            return

        result = await self.provider.answer_inbox(
            question, context.text, preferred_language=preferred_language
        )
        payload, failure = _validated_answer(result, context)
        if failure == "insufficient":
            payload = _controlled_answer("INSUFFICIENT_CONTEXT", preferred_language)
        elif failure is not None:
            log.info("ask citation validation retry job=%s reason=%s", ask_job_id, failure)
            result = await self.provider.answer_inbox(
                question, context.text, preferred_language=preferred_language
            )
            payload, retry_failure = _validated_answer(result, context)
            if retry_failure == "insufficient" or retry_failure is not None:
                payload = _controlled_answer("INSUFFICIENT_CONTEXT", preferred_language)
                log.info(
                    "ask citation validation exhausted job=%s reason=%s",
                    ask_job_id,
                    retry_failure,
                )
        await self._complete(ask_job_id, user_id, payload)

    async def _complete(self, ask_job_id: int, user_id: int, payload: AskDeliveryPayload) -> None:
        from app.services.delivery import ASK_RESULT, enqueue_ask_delivery

        async with self.session_factory() as session:
            job = await session.get(AskJob, ask_job_id)
            if job is None or job.status != "RUNNING":
                return
            job.status = "DONE"
            job.error_code = None
            job.error_message = None
            await enqueue_ask_delivery(
                session,
                user_id=user_id,
                ask_job_id=ask_job_id,
                delivery_type=ASK_RESULT,
                payload=payload.model_dump(mode="json"),
            )
            await session.commit()

    async def fail(self, ask_job_id: int, user_id: int, code: str) -> None:
        """Store a safe failure and delivery intent, not provider exception text."""
        from app.services.delivery import ASK_FAILED, enqueue_ask_delivery

        async with self.session_factory() as session:
            job = await session.get(AskJob, ask_job_id)
            if job is None or job.status != "RUNNING":
                return
            job.status = "FAILED"
            job.error_code = code[:64]
            job.error_message = "Ask computation failed"
            await enqueue_ask_delivery(
                session,
                user_id=user_id,
                ask_job_id=ask_job_id,
                delivery_type=ASK_FAILED,
                payload={},
            )
            await session.commit()


def ask_failure_code(exc: Exception) -> str:
    """Keep technical diagnostics compact and avoid persisting private provider text."""
    return exc.code if isinstance(exc, AppError) else "ASK_FAILED"
