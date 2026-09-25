"""Ground and persist contextual hooks used by PM-08's proactive reminders."""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select, text

from app.domain.enums import AttentionHookType, ContentKind
from app.domain.models import AttentionHook, AttentionHookCandidate, AttentionHookGeneration
from app.llm.base import AttentionHookGenerationResult, LlmError, LlmProvider
from app.services.analysis import split_text
from app.services.profile import get_profile
from app.storage.models import Content, Item, ItemSource, Reminder

log = logging.getLogger(__name__)

ATTENTION_HOOK_GENERATOR_VERSION = 2
MAX_HOOK_CONTEXT_CHARS = 12_000
HOOK_CONTEXT_CHUNK_CHARS = 1_500
MAX_HOOK_CHUNKS_PER_SOURCE = 4
MAX_STORED_HOOKS = 3

_ALLOWED_EVIDENCE_KINDS = frozenset(
    {
        ContentKind.USER_TEXT,
        ContentKind.WEB_TEXT,
        ContentKind.DOCUMENT_TEXT,
        ContentKind.TRANSCRIPT,
        ContentKind.VISUAL_NOTES,
        ContentKind.DESCRIPTION,
    }
)
# ❌ Удалены три общие обёртки: они добавляли слова, но не давали новой причины
# открыть материал; история Reminder теперь ротирует сами grounded hooks.
_HOOK_TEMPLATES = (("direct_v2", "{hook}"),)


@dataclass(frozen=True)
class AttentionHookPresentation:
    """Small immutable handoff from domain service to Telegram presentation."""

    hook: AttentionHook
    template_id: str
    rendered_text: str


@dataclass(frozen=True)
class _HookContext:
    """Bounded prompt projection retaining the exact Content IDs and excerpts sent."""

    text: str
    excerpts_by_content_id: dict[int, tuple[str, ...]]


@dataclass(frozen=True)
class _InitialState:
    """Read-only snapshot used before provider I/O, with no open SQLite session."""

    hooks: tuple[AttentionHook, ...]
    context: _HookContext | None
    preferred_language: str


@dataclass(frozen=True)
class _ValidatedCandidate:
    """Candidate whose evidence and source provenance passed application checks."""

    source_id: int | None
    hook_type: AttentionHookType
    text: str
    evidence_content_id: int
    evidence_excerpt: str


class AttentionHookService:
    """Own lazy generation, grounding, reuse and Reminder attribution outside Telegram."""

    def __init__(self, session_factory, provider: LlmProvider):
        self.session_factory = session_factory
        self.provider = provider

    async def for_reminder(
        self,
        *,
        item_id: int,
        user_id: int,
        reminder_id: int,
        claim_generation: int,
        timeout_seconds: float,
    ) -> AttentionHookPresentation | None:
        """Resolve one persisted hook pair before PM-08's final delivery revalidation."""
        loop = asyncio.get_running_loop()
        generation_deadline = loop.time() + max(0.0, timeout_seconds)
        initial = await self._load_initial_state(item_id, user_id)
        if initial is None:
            return None

        generation_result = None
        if not initial.hooks and initial.context and initial.context.excerpts_by_content_id:
            provider_timeout = generation_deadline - loop.time()
            if provider_timeout > 0:
                try:
                    async with asyncio.timeout(provider_timeout):
                        generation_result = await self.provider.generate_attention_hooks(
                            initial.context.text,
                            preferred_language=initial.preferred_language,
                        )
                    if (
                        not isinstance(generation_result, AttentionHookGenerationResult)
                        or not isinstance(generation_result.generation, AttentionHookGeneration)
                        or not isinstance(generation_result.provider, str)
                        or not isinstance(generation_result.model, str)
                    ):
                        log.warning(
                            "attention hook fallback item_id=%s reminder_id=%s "
                            "reason=invalid_result",
                            item_id,
                            reminder_id,
                        )
                        generation_result = None
                except TimeoutError:
                    log.info(
                        "attention hook fallback item_id=%s reminder_id=%s reason=timeout",
                        item_id,
                        reminder_id,
                    )
                except LlmError as exc:
                    log.info(
                        "attention hook fallback item_id=%s reminder_id=%s error_code=%s",
                        item_id,
                        reminder_id,
                        exc.code,
                    )
                except Exception as exc:
                    # Provider failures are optional enhancement failures; DB work is outside
                    # this block so storage errors keep the worker's normal failure semantics.
                    log.warning(
                        "attention hook fallback item_id=%s reminder_id=%s error_type=%s",
                        item_id,
                        reminder_id,
                        type(exc).__name__,
                    )
            else:
                log.info(
                    "attention hook fallback item_id=%s reminder_id=%s reason=send_deadline",
                    item_id,
                    reminder_id,
                )

        return await self._persist_and_select(
            item_id=item_id,
            user_id=user_id,
            reminder_id=reminder_id,
            claim_generation=claim_generation,
            context=initial.context,
            generation_result=generation_result,
        )

    async def _load_initial_state(self, item_id: int, user_id: int) -> _InitialState | None:
        """Read persisted evidence and release SQLite before any provider request."""
        async with self.session_factory() as session:
            state = await self._load_item_content(session, item_id, user_id)
            if state is None:
                return None
            contents, sources = state
            source_ids = frozenset(source.id for source in sources if source.id is not None)
            hooks = tuple(
                self._valid_stored_hooks(item_id, contents, source_ids)[:MAX_STORED_HOOKS]
            )
            if hooks:
                return _InitialState(hooks, None, "ru")

            context = _build_hook_context(contents, sources)
            if not context.excerpts_by_content_id:
                return _InitialState((), context, "ru")
            profile = await get_profile(session, user_id)
            return _InitialState(
                (),
                context,
                profile.preferred_language,
            )

    async def _load_item_content(
        self, session, item_id: int, user_id: int
    ) -> tuple[list[Content], list[ItemSource]] | None:
        """Load canonical source text only when the Item belongs to this user."""
        item = await session.get(Item, item_id)
        if item is None or item.user_id != user_id:
            return None
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item_id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )
        contents = list(
            (
                await session.scalars(
                    select(Content).where(Content.item_id == item_id).order_by(Content.id)
                )
            ).all()
        )
        return contents, sources

    def _valid_stored_hooks(
        self, item_id: int, contents: list[Content], source_ids: frozenset[int]
    ) -> list[AttentionHook]:
        """Revalidate every reusable hook against current source rows and provenance."""
        contents_by_id = {content.id: content for content in contents}
        valid = []
        for row in contents:
            if row.kind is not ContentKind.ATTENTION_HOOK:
                continue
            hook = _stored_hook(row, item_id, contents_by_id, source_ids)
            if hook is not None and hook.generator_version == ATTENTION_HOOK_GENERATOR_VERSION:
                valid.append(hook)
        return sorted(valid, key=lambda hook: hook.content_id)

    async def _persist_and_select(
        self,
        *,
        item_id: int,
        user_id: int,
        reminder_id: int,
        claim_generation: int,
        context: _HookContext | None,
        generation_result: AttentionHookGenerationResult | None,
    ) -> AttentionHookPresentation | None:
        """Serialize short hook writes and reminder attribution, fencing recovered claims."""
        async with self.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            reminder = await session.get(Reminder, reminder_id)
            if (
                reminder is None
                or reminder.user_id != user_id
                or reminder.item_id != item_id
                or reminder.type != "PROACTIVE_ATTENTION"
                or reminder.status != "CLAIMED"
                or reminder.claim_generation != claim_generation
            ):
                await session.commit()
                return None

            state = await self._load_item_content(session, item_id, user_id)
            if state is None:
                await session.commit()
                return None
            contents, sources = state
            source_ids = frozenset(source.id for source in sources if source.id is not None)
            hooks = self._valid_stored_hooks(item_id, contents, source_ids)

            if not hooks and generation_result is not None and context is not None:
                validated = _validated_candidates(
                    generation_result.generation.candidates,
                    item_id=item_id,
                    contents=contents,
                    source_ids=source_ids,
                    context=context,
                )
                inserted_rows: list[tuple[Content, _ValidatedCandidate]] = []
                for candidate in _diverse_candidates(validated):
                    metadata = {
                        "hook_type": candidate.hook_type.value,
                        "evidence_content_id": candidate.evidence_content_id,
                        "evidence_excerpt": candidate.evidence_excerpt,
                        "generator_version": ATTENTION_HOOK_GENERATOR_VERSION,
                    }
                    if generation_result.provider:
                        metadata["provider"] = generation_result.provider[:64]
                    if generation_result.model:
                        metadata["model"] = generation_result.model[:160]
                    row = Content(
                        item_id=item_id,
                        source_id=candidate.source_id,
                        kind=ContentKind.ATTENTION_HOOK,
                        text=candidate.text,
                        metadata_json=metadata,
                    )
                    session.add(row)
                    inserted_rows.append((row, candidate))
                if inserted_rows:
                    await session.flush()
                    hooks = [
                        AttentionHook(
                            content_id=row.id,
                            item_id=item_id,
                            source_id=candidate.source_id,
                            hook_type=candidate.hook_type,
                            text=candidate.text,
                            evidence_content_id=candidate.evidence_content_id,
                            evidence_excerpt=candidate.evidence_excerpt,
                            generator_version=ATTENTION_HOOK_GENERATOR_VERSION,
                        )
                        for row, candidate in inserted_rows
                    ]

            hooks = sorted(hooks, key=lambda hook: hook.content_id)[:MAX_STORED_HOOKS]
            payload = dict(reminder.payload_json or {})
            presentation = await self._select_presentation(session, reminder, hooks, payload)
            if presentation is None:
                payload.pop("hook_content_id", None)
                payload.pop("template_id", None)
                payload.pop("focus_source_id", None)
            else:
                payload["hook_content_id"] = presentation.hook.content_id
                payload["template_id"] = presentation.template_id
                if presentation.hook.source_id is None:
                    payload.pop("focus_source_id", None)
                else:
                    # PM-11 restores the same relevant-source-first keyboard if
                    # the user opens and then cancels reminder snooze selection.
                    payload["focus_source_id"] = presentation.hook.source_id
            if payload != (reminder.payload_json or {}):
                reminder.payload_json = payload
            await session.commit()
            if presentation is not None:
                log.info(
                    "attention hook selected item_id=%s reminder_id=%s hook_content_id=%s "
                    "hook_type=%s generator_version=%s",
                    item_id,
                    reminder_id,
                    presentation.hook.content_id,
                    presentation.hook.hook_type.value,
                    presentation.hook.generator_version,
                )
            return presentation

    async def _select_presentation(
        self, session, reminder: Reminder, hooks: list[AttentionHook], payload: dict
    ) -> AttentionHookPresentation | None:
        """Keep a recovered Reminder's valid pair, otherwise rotate past its latest sent pair."""
        templates = dict(_HOOK_TEMPLATES)
        by_id = {hook.content_id: hook for hook in hooks}
        stored_id = payload.get("hook_content_id")
        stored_template = payload.get("template_id")
        if (
            type(stored_id) is int
            and isinstance(stored_template, str)
            and stored_template in templates
            and stored_id in by_id
        ):
            return _render_presentation(by_id[stored_id], stored_template, templates)
        if not hooks:
            return None

        latest_payload = await session.scalar(
            select(Reminder.payload_json)
            .where(
                Reminder.user_id == reminder.user_id,
                Reminder.item_id == reminder.item_id,
                Reminder.type == "PROACTIVE_ATTENTION",
                Reminder.status == "SENT",
                Reminder.sent_at.is_not(None),
            )
            .order_by(Reminder.sent_at.desc(), Reminder.id.desc())
            .limit(1)
        )
        last_pair = None
        if isinstance(latest_payload, dict):
            last_id = latest_payload.get("hook_content_id")
            last_template = latest_payload.get("template_id")
            if type(last_id) is int and isinstance(last_template, str):
                last_pair = (last_id, last_template)

        pairs = [
            (hook, template_id) for hook in hooks for template_id, _template in _HOOK_TEMPLATES
        ]
        chosen = next(
            (pair for pair in pairs if (pair[0].content_id, pair[1]) != last_pair),
            pairs[0],
        )
        return _render_presentation(chosen[0], chosen[1], templates)


def _build_hook_context(contents: list[Content], sources: list[ItemSource]) -> _HookContext:
    """Project original Content into a fair, deterministic and globally bounded prompt."""
    source_order = [None] + [source.id for source in sources if source.id is not None]
    owned_source_ids = {source.id for source in sources if source.id is not None}
    grouped: dict[int | None, list[list[_EvidenceChunk]]] = {
        source_id: [] for source_id in source_order
    }
    for content in contents:
        if content.kind not in _ALLOWED_EVIDENCE_KINDS:
            continue
        if content.source_id is not None and content.source_id not in owned_source_ids:
            continue
        excerpts = _representative_chunks(content.text)
        if excerpts:
            grouped.setdefault(content.source_id, []).append(
                [
                    _EvidenceChunk(content.id, content.source_id, content.kind, excerpt)
                    for excerpt in excerpts
                ]
            )

    # Multiple content kinds for one ItemSource share four slots; round-robin
    # prevents a long transcript from hiding its description or visual notes.
    chunks_by_source: dict[int | None, list[_EvidenceChunk]] = {}
    for source_id in source_order:
        content_chunks = grouped.get(source_id, [])
        selected: list[_EvidenceChunk] = []
        seen_text: set[str] = set()
        slot = 0
        while len(selected) < MAX_HOOK_CHUNKS_PER_SOURCE:
            added = False
            for chunks in content_chunks:
                if slot >= len(chunks):
                    continue
                chunk = chunks[slot]
                normalized = _normalize_whitespace(chunk.text)
                if normalized and normalized not in seen_text:
                    selected.append(chunk)
                    seen_text.add(normalized)
                    added = True
                    if len(selected) == MAX_HOOK_CHUNKS_PER_SOURCE:
                        break
            if not added:
                break
            slot += 1
        if selected:
            chunks_by_source[source_id] = selected

    available_sources = [source_id for source_id in source_order if source_id in chunks_by_source]
    ordered_sources = _fair_source_order(available_sources)

    excerpts_by_content_id: dict[int, list[str]] = {}
    parts: list[str] = []
    used = 0
    positions = {source_id: 0 for source_id in ordered_sources}
    while True:
        progressed = False
        for source_id in ordered_sources:
            position = positions[source_id]
            candidates = chunks_by_source[source_id]
            if position >= len(candidates):
                continue
            progressed = True
            positions[source_id] += 1
            chunk = candidates[position]
            block = (
                f"CONTENT_ID: {chunk.content_id}\n"
                f"SOURCE_ID: {chunk.source_id if chunk.source_id is not None else 'null'}\n"
                f"KIND: {chunk.kind.value}\n"
                f"EXCERPT:\n{chunk.text}"
            )
            extra = len(block) + (2 if parts else 0)
            if used + extra > MAX_HOOK_CONTEXT_CHARS:
                continue
            parts.append(block)
            used += extra
            excerpts_by_content_id.setdefault(chunk.content_id, []).append(chunk.text)
        if not progressed:
            break

    return _HookContext(
        text="\n\n".join(parts),
        excerpts_by_content_id={
            content_id: tuple(excerpts) for content_id, excerpts in excerpts_by_content_id.items()
        },
    )


def _fair_source_order(source_ids: list[int | None]) -> list[int | None]:
    """Interleave both ends of source order so bounded prompts include later sources."""
    ordered: list[int | None] = []
    left, right = 0, len(source_ids) - 1
    while left <= right:
        ordered.append(source_ids[left])
        if left != right:
            ordered.append(source_ids[right])
        left += 1
        right -= 1
    return ordered


@dataclass(frozen=True)
class _EvidenceChunk:
    """One representative excerpt with the original Content and ItemSource identity."""

    content_id: int
    source_id: int | None
    kind: ContentKind
    text: str


def _representative_chunks(value: str) -> list[str]:
    """Select at most four deterministic regions without duplicating chunk text."""
    chunks = split_text(value, HOOK_CONTEXT_CHUNK_CHARS)
    if len(chunks) <= MAX_HOOK_CHUNKS_PER_SOURCE:
        indices = range(len(chunks))
    else:
        indices = sorted({0, len(chunks) // 3, 2 * len(chunks) // 3, len(chunks) - 1})
    selected: list[str] = []
    seen: set[str] = set()
    for index in indices:
        chunk = chunks[index]
        normalized = _normalize_whitespace(chunk)
        if normalized and normalized not in seen:
            selected.append(chunk)
            seen.add(normalized)
    return selected


def _validated_candidates(
    candidates: list[AttentionHookCandidate],
    *,
    item_id: int,
    contents: list[Content],
    source_ids: frozenset[int],
    context: _HookContext,
) -> list[_ValidatedCandidate]:
    """Accept only candidates whose exact supporting excerpt was sent and still exists."""
    contents_by_id = {content.id: content for content in contents}
    validated = []
    for candidate in candidates[:MAX_STORED_HOOKS]:
        if not isinstance(candidate.hook_type, AttentionHookType):
            continue
        source_content_id = candidate.source_content_id
        if (
            type(source_content_id) is not int
            or source_content_id not in context.excerpts_by_content_id
        ):
            continue
        evidence = contents_by_id.get(source_content_id)
        excerpt = candidate.evidence_excerpt
        excerpt_normalized = _normalize_whitespace(excerpt)
        hook_text = candidate.text.strip()
        if (
            evidence is None
            or evidence.item_id != item_id
            or evidence.kind not in _ALLOWED_EVIDENCE_KINDS
            or not excerpt_normalized
            or len(excerpt) > 300
            or not hook_text
            or len(hook_text) > 280
            or excerpt_normalized not in _normalize_whitespace(evidence.text)
            or not any(
                excerpt_normalized in _normalize_whitespace(supplied_excerpt)
                for supplied_excerpt in context.excerpts_by_content_id[source_content_id]
            )
            or (evidence.source_id is not None and evidence.source_id not in source_ids)
        ):
            continue
        validated.append(
            _ValidatedCandidate(
                source_id=evidence.source_id,
                hook_type=candidate.hook_type,
                text=hook_text,
                evidence_content_id=evidence.id,
                evidence_excerpt=excerpt,
            )
        )
    return validated


def _stored_hook(
    row: Content,
    item_id: int,
    contents_by_id: dict[int, Content],
    source_ids: frozenset[int],
) -> AttentionHook | None:
    """Reconstruct a reusable DTO only while its original evidence remains valid."""
    metadata = row.metadata_json
    if not isinstance(metadata, dict):
        return None
    hook_type_value = metadata.get("hook_type")
    evidence_content_id = metadata.get("evidence_content_id")
    evidence_excerpt = metadata.get("evidence_excerpt")
    generator_version = metadata.get("generator_version")
    if (
        type(evidence_content_id) is not int
        or not isinstance(evidence_excerpt, str)
        or not 0 < len(evidence_excerpt) <= 300
        or type(generator_version) is not int
        or not row.text.strip()
        or len(row.text) > 280
    ):
        return None
    try:
        hook_type = AttentionHookType(hook_type_value)
    except (TypeError, ValueError):
        return None
    evidence = contents_by_id.get(evidence_content_id)
    normalized_excerpt = _normalize_whitespace(evidence_excerpt)
    if (
        row.item_id != item_id
        or row.kind is not ContentKind.ATTENTION_HOOK
        or evidence is None
        or evidence.item_id != item_id
        or evidence.kind not in _ALLOWED_EVIDENCE_KINDS
        or not normalized_excerpt
        or normalized_excerpt not in _normalize_whitespace(evidence.text)
        or row.source_id != evidence.source_id
        or (evidence.source_id is not None and evidence.source_id not in source_ids)
    ):
        return None
    return AttentionHook(
        content_id=row.id,
        item_id=item_id,
        source_id=evidence.source_id,
        hook_type=hook_type,
        text=row.text.strip(),
        evidence_content_id=evidence.id,
        evidence_excerpt=evidence_excerpt,
        generator_version=generator_version,
    )


def _diverse_candidates(candidates: list[_ValidatedCandidate]) -> list[_ValidatedCandidate]:
    """Persist at most one hook per semantic frame instead of filling slots with paraphrases."""
    unique: list[_ValidatedCandidate] = []
    seen_text: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_whitespace(candidate.text).casefold()
        if normalized and normalized not in seen_text:
            seen_text.add(normalized)
            unique.append(candidate)
    selected: list[_ValidatedCandidate] = []
    seen_types: set[AttentionHookType] = set()
    for candidate in unique:
        if candidate.hook_type not in seen_types:
            selected.append(candidate)
            seen_types.add(candidate.hook_type)
            if len(selected) == MAX_STORED_HOOKS:
                return selected
    # ❌ Удалено заполнение квоты однотипными кандидатами: дополнительные места
    # полезнее оставить пустыми, чем закрепить несколько перефразировок одного frame.
    return selected


def _render_presentation(
    hook: AttentionHook, template_id: str, templates: dict[str, str]
) -> AttentionHookPresentation:
    """Bind one semantic hook to a stable deterministic presentation template."""
    return AttentionHookPresentation(
        hook=hook,
        template_id=template_id,
        rendered_text=templates[template_id].format(hook=hook.text),
    )


def _normalize_whitespace(value: str) -> str:
    """Ignore only whitespace differences while keeping evidence case and punctuation exact."""
    return " ".join(value.split())
