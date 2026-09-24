"""PM-09 grounding and bounded-generation regressions."""

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.domain.enums import (
    AttentionHookType,
    ContentKind,
    ItemState,
    ItemType,
    ProcessingStatus,
    SourceType,
)
from app.domain.models import AttentionHookCandidate, AttentionHookGeneration
from app.llm.base import LlmError
from app.llm.openai import OpenAiProvider, strict_json_schema
from app.services.attention_hooks import (
    ATTENTION_HOOK_GENERATOR_VERSION,
    MAX_HOOK_CHUNKS_PER_SOURCE,
    MAX_HOOK_CONTEXT_CHARS,
    AttentionHookService,
)
from app.storage.models import Content, Item, ItemSource, Reminder, User
from tests.fakes import FakeLlmProvider


async def _seed_claimed_item(
    session_factory,
    *,
    text: str = "A study cut latency by 20% after caching.",
    kind: ContentKind = ContentKind.WEB_TEXT,
    with_source: bool = True,
    preferred_language: str = "ru",
) -> tuple[int, int, int | None, int, datetime]:
    """Create persisted source evidence and a claimed reminder for service-level tests."""
    async with session_factory() as session:
        user = User(
            telegram_user_id=42,
            telegram_chat_id=42,
            profile_json={"preferred_language": preferred_language},
        )
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.WEB if with_source else SourceType.TEXT,
            processing_stage="READY",
            user_note="not part of persisted source Content",
            item_type=ItemType.READ,
            title="Saved article",
            priority_score=80,
        )
        session.add(item)
        await session.flush()
        source = None
        if with_source:
            source = ItemSource(
                item_id=item.id,
                source_index=0,
                source_type=SourceType.WEB,
                source_url="https://example.com/article",
                extraction_status="READY",
            )
            session.add(source)
            await session.flush()
        evidence = Content(
            item_id=item.id,
            source_id=source.id if source is not None else None,
            kind=kind,
            text=text,
        )
        session.add(evidence)
        await session.flush()
        claimed_at = datetime.now(UTC).replace(tzinfo=None)
        reminder = Reminder(
            user_id=user.id,
            item_id=item.id,
            type="PROACTIVE_ATTENTION",
            scheduled_at=claimed_at,
            status="CLAIMED",
            claimed_at=claimed_at,
            claim_generation=1,
            payload_json={"attention_score": 81, "priority_score": 80, "policy_level": 3},
        )
        session.add(reminder)
        await session.commit()
        return user.id, item.id, source.id if source is not None else None, evidence.id, claimed_at


def _candidate(content_id: int, evidence: str, text: str = "Caching reduced latency."):
    """Build the strict provider contract used by grounding tests."""
    return AttentionHookCandidate(
        hook_type=AttentionHookType.PRACTICAL_VALUE,
        text=text,
        evidence_excerpt=evidence,
        source_content_id=content_id,
    )


async def test_provider_uses_strict_schema_and_returns_explicit_identity():
    """Keep Structured Outputs and OpenRouter identity inside the adapter boundary."""
    evidence_id = 71
    generation = AttentionHookGeneration(
        candidates=[_candidate(evidence_id, "A study cut latency by 20%")]
    )

    class FakeCompletions:
        """Capture requests while returning deterministic provider JSON."""

        def __init__(self):
            self.responses = [generation.model_dump_json(), '{"candidates": [], "extra": 1}']
            self.requests = []

        async def create(self, **kwargs):
            self.requests.append(kwargs)
            raw = self.responses.pop(0)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])

    provider = OpenAiProvider(api_key="test", model="router/model", provider_name="openrouter")
    completions = FakeCompletions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await provider.generate_attention_hooks("CONTENT_ID: 71", preferred_language="en")
    assert result.generation == generation
    assert result.provider == "openrouter"
    assert result.model == "router/model"
    request = completions.requests[0]
    assert request["response_format"]["json_schema"]["strict"] is True
    assert "CONTENT_ID: 71" in request["messages"][1]["content"]
    assert "RESPONSE LANGUAGE: en" in request["messages"][1]["content"]
    assert "untrusted data" in request["messages"][0]["content"]
    assert strict_json_schema(AttentionHookGeneration)["additionalProperties"] is False

    with pytest.raises(LlmError) as exc_info:
        await provider.generate_attention_hooks("context", preferred_language="ru")
    assert exc_info.value.code == "INVALID_LLM_OUTPUT"
    assert len(completions.requests) == 2


def test_attention_hook_schema_rejects_extra_type_and_oversized_fields():
    """Reject malformed structured output before it reaches application persistence."""
    valid = {
        "hook_type": "QUESTION",
        "text": "Why did the change work?",
        "evidence_excerpt": "The cache reduced response time.",
        "source_content_id": 5,
    }
    with pytest.raises(ValidationError):
        AttentionHookCandidate(**{**valid, "unknown": True})
    with pytest.raises(ValidationError):
        AttentionHookCandidate(**{**valid, "hook_type": "UNKNOWN"})
    with pytest.raises(ValidationError):
        AttentionHookCandidate(**{**valid, "text": "x" * 321})
    with pytest.raises(ValidationError):
        AttentionHookCandidate(**{**valid, "evidence_excerpt": "x" * 301})
    with pytest.raises(ValidationError):
        AttentionHookGeneration(candidates=[valid, valid, valid, valid])


async def test_generation_persists_grounded_hook_and_only_passes_response_language(
    session_factory,
):
    """Persist semantic hooks with canonical source provenance and provider attribution."""
    user_id, item_id, source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory, preferred_language="en"
    )
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, "A study cut latency by 20%")]
        )
    )
    service = AttentionHookService(session_factory, provider)

    presentation = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert presentation is not None
    assert presentation.hook.source_id == source_id
    assert presentation.hook.evidence_content_id == content_id
    assert presentation.hook.generator_version == ATTENTION_HOOK_GENERATOR_VERSION
    assert presentation.template_id == "strong_thought_v1"
    assert "Caching reduced latency." in presentation.rendered_text
    prompt, language = provider.attention_hook_calls[0]
    assert language == "en"
    assert f"CONTENT_ID: {content_id}" in prompt
    assert "not part of persisted source Content" not in prompt
    assert len(provider.attention_hook_calls) == 1

    async with session_factory() as session:
        hook = await session.scalar(
            select(Content).where(
                Content.item_id == item_id,
                Content.kind == ContentKind.ATTENTION_HOOK,
            )
        )
        reminder = await session.get(Reminder, 1)
        assert hook.source_id == source_id
        assert hook.text == "Caching reduced latency."
        assert hook.metadata_json == {
            "hook_type": "PRACTICAL_VALUE",
            "evidence_content_id": content_id,
            "evidence_excerpt": "A study cut latency by 20%",
            "generator_version": ATTENTION_HOOK_GENERATOR_VERSION,
            "provider": "fake",
            "model": "fake-model",
        }
        assert reminder.payload_json["hook_content_id"] == hook.id
        assert reminder.payload_json["template_id"] == presentation.template_id
        assert "evidence_excerpt" not in reminder.payload_json
        assert reminder.claimed_at is not None


@pytest.mark.parametrize(
    "excerpt",
    [
        "A study cut latency by 80%",  # fabricated statistic
        "Исследование снизило задержку на 20%",  # translated evidence
        "a study cut latency by 20%",  # case changes are not whitespace normalization
    ],
)
async def test_unextractive_or_translated_evidence_is_rejected(session_factory, excerpt):
    """Require exact source-language evidence after whitespace-only normalization."""
    user_id, item_id, _source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, excerpt)]
        )
    )
    service = AttentionHookService(session_factory, provider)

    result = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert result is None
    assert len(provider.attention_hook_calls) == 1
    async with session_factory() as session:
        count = await session.scalar(
            select(Content.id).where(Content.kind == ContentKind.ATTENTION_HOOK)
        )
        assert count is None


async def test_content_from_another_item_cannot_support_a_hook(session_factory):
    """Reject a real Content ID from another Item even if its excerpt is exact."""
    user_id, item_id, _source_id, _content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    async with session_factory() as session:
        other_item = Item(
            user_id=user_id,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.WEB,
            processing_stage="READY",
            user_note="",
            item_type=ItemType.READ,
            title="Other item",
        )
        session.add(other_item)
        await session.flush()
        foreign_content = Content(
            item_id=other_item.id,
            kind=ContentKind.WEB_TEXT,
            text="A foreign Item has the same-looking evidence.",
        )
        session.add(foreign_content)
        await session.commit()
        foreign_content_id = foreign_content.id
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[
                _candidate(foreign_content_id, "A foreign Item has the same-looking evidence.")
            ]
        )
    )

    result = await AttentionHookService(session_factory, provider).for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert result is None
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(Content.id).where(Content.kind == ContentKind.ATTENTION_HOOK)
            )
            is None
        )


async def test_whitespace_only_evidence_differences_and_item_text_provenance_are_valid(
    session_factory,
):
    """Allow harmless whitespace changes while keeping item-level evidence source-less."""
    user_id, item_id, source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory,
        text="A result\n  improved latency by 20%.",
        kind=ContentKind.USER_TEXT,
        with_source=False,
    )
    assert source_id is None
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, "A result improved latency by 20%.")]
        )
    )
    service = AttentionHookService(session_factory, provider)

    result = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert result is not None
    assert result.hook.source_id is None
    async with session_factory() as session:
        hook = await session.scalar(
            select(Content).where(Content.kind == ContentKind.ATTENTION_HOOK)
        )
        assert hook.source_id is None


@pytest.mark.parametrize(
    "kind", [ContentKind.CHUNK_SUMMARY, ContentKind.TRANSCRIPT_CHUNK, ContentKind.ATTENTION_HOOK]
)
async def test_derived_and_retry_content_cannot_be_hook_evidence(session_factory, kind):
    """Keep generated hooks and intermediate summaries outside original evidence context."""
    user_id, item_id, _source_id, _content_id, _claimed_at = await _seed_claimed_item(
        session_factory, kind=kind
    )
    provider = FakeLlmProvider()
    result = await AttentionHookService(session_factory, provider).for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    assert result is None
    assert provider.attention_hook_calls == []


async def test_bounded_context_is_labeled_fair_and_caps_chunks_per_source(session_factory):
    """Represent later composite sources under a global prompt bound without starving them."""
    async with session_factory() as session:
        user = User(telegram_user_id=42, telegram_chat_id=42)
        session.add(user)
        await session.flush()
        item = Item(
            user_id=user.id,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.WEB,
            processing_stage="READY",
            user_note="",
            item_type=ItemType.READ,
            title="Composite article",
            priority_score=80,
        )
        session.add(item)
        await session.flush()
        sources = []
        for index in range(12):
            source = ItemSource(
                item_id=item.id,
                source_index=index,
                source_type=SourceType.WEB,
                source_url=f"https://example.com/{index}",
                extraction_status="READY",
            )
            session.add(source)
            sources.append(source)
        await session.flush()
        source_contents = [
            Content(
                item_id=item.id,
                source_id=source.id,
                kind=ContentKind.WEB_TEXT,
                text=f"SOURCE_MARKER_{index} " + (f"word{index} " * 8_000),
            )
            for index, source in enumerate(sources)
        ]
        session.add_all(source_contents)
        await session.flush()
        session.add(
            Reminder(
                user_id=user.id,
                item_id=item.id,
                type="PROACTIVE_ATTENTION",
                scheduled_at=datetime.now(UTC).replace(tzinfo=None),
                status="CLAIMED",
                claimed_at=datetime.now(UTC).replace(tzinfo=None),
                claim_generation=1,
            )
        )
        await session.commit()
        user_id, item_id = user.id, item.id
        source_ids = [source.id for source in sources]

    provider = FakeLlmProvider()
    await AttentionHookService(session_factory, provider).for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    context = provider.attention_hook_calls[0][0]
    source_labels = [int(value) for value in re.findall(r"SOURCE_ID: (\d+)", context)]
    counts = Counter(source_labels)
    assert len(context) <= MAX_HOOK_CONTEXT_CHARS
    assert max(counts.values()) <= MAX_HOOK_CHUNKS_PER_SOURCE
    assert set(counts) & set(source_ids)
    assert source_ids[-1] in counts
    assert counts[source_ids[0]] <= MAX_HOOK_CHUNKS_PER_SOURCE
    assert "CONTENT_ID:" in context and "KIND: WEB_TEXT" in context and "EXCERPT:" in context


async def test_multiple_content_kinds_share_four_slots_for_one_source(session_factory):
    """Represent transcript and visual notes without treating them as separate sources."""
    user_id, item_id, source_id, _content_id, _claimed_at = await _seed_claimed_item(
        session_factory,
        text="TRANSCRIPT_MARKER " + ("transcriptword " * 8_000),
        kind=ContentKind.TRANSCRIPT,
    )
    async with session_factory() as session:
        visual = Content(
            item_id=item_id,
            source_id=source_id,
            kind=ContentKind.VISUAL_NOTES,
            text="VISUAL_MARKER " + ("visualword " * 8_000),
        )
        session.add(visual)
        await session.commit()
        visual_id = visual.id
        transcript_id = await session.scalar(
            select(Content.id).where(
                Content.item_id == item_id,
                Content.kind == ContentKind.TRANSCRIPT,
            )
        )
    provider = FakeLlmProvider()

    await AttentionHookService(session_factory, provider).for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    context = provider.attention_hook_calls[0][0]
    source_labels = [int(value) for value in re.findall(r"SOURCE_ID: (\d+)", context)]
    content_labels = {int(value) for value in re.findall(r"CONTENT_ID: (\d+)", context)}
    assert source_labels.count(source_id) <= MAX_HOOK_CHUNKS_PER_SOURCE
    assert transcript_id in content_labels
    assert visual_id in content_labels


async def test_valid_hook_reuse_and_recovery_preserve_selected_pair(session_factory):
    """Reuse current evidence without provider work and keep a claimed Reminder's wording."""
    user_id, item_id, _source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, "A study cut latency by 20%")]
        )
    )
    service = AttentionHookService(session_factory, provider)
    first = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    assert first is not None
    async with session_factory() as session:
        reminder = await session.get(Reminder, 1)
        payload = dict(reminder.payload_json)
        payload["template_id"] = "saved_long_ago_v1"
        reminder.payload_json = payload
        await session.commit()

    recovered = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    assert recovered is not None
    assert recovered.hook.content_id == first.hook.content_id
    assert recovered.template_id == "saved_long_ago_v1"
    assert len(provider.attention_hook_calls) == 1


async def test_success_history_avoids_immediate_pair_and_failed_pair_does_not_count(
    session_factory,
):
    """Rotate deterministic hook/template pairs from successful Reminder history only."""
    user_id, item_id, _source_id, content_id, claimed_at = await _seed_claimed_item(session_factory)
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, "A study cut latency by 20%")]
        )
    )
    service = AttentionHookService(session_factory, provider)
    first = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    assert first is not None
    async with session_factory() as session:
        session.add(
            Reminder(
                user_id=user_id,
                item_id=item_id,
                type="PROACTIVE_ATTENTION",
                scheduled_at=claimed_at - timedelta(seconds=2),
                status="SENT",
                sent_at=claimed_at - timedelta(seconds=1),
                payload_json={
                    "hook_content_id": first.hook.content_id,
                    "template_id": "strong_thought_v1",
                },
            )
        )
        session.add(
            Reminder(
                user_id=user_id,
                item_id=item_id,
                type="PROACTIVE_ATTENTION",
                scheduled_at=claimed_at - timedelta(seconds=4),
                status="FAILED",
                payload_json={
                    "hook_content_id": first.hook.content_id,
                    "template_id": "reason_to_return_v1",
                },
            )
        )
        reminder = await session.get(Reminder, 1)
        reminder.payload_json = dict(reminder.payload_json)
        reminder.payload_json.pop("hook_content_id", None)
        reminder.payload_json.pop("template_id", None)
        await session.commit()

    next_presentation = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    assert next_presentation is not None
    assert next_presentation.template_id == "reason_to_return_v1"
    assert len(provider.attention_hook_calls) == 1


async def test_changed_evidence_is_ignored_and_regenerated_without_mutating_old_hook(
    session_factory,
):
    """Treat changed source evidence as stale while preserving the old derived record."""
    user_id, item_id, _source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, "A study cut latency by 20%")]
        )
    )
    service = AttentionHookService(session_factory, provider)
    first = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    async with session_factory() as session:
        source_content = await session.get(Content, content_id)
        source_content.text = "A revised study cut latency by 30% after caching."
        await session.commit()
    provider.attention_hook_generation = AttentionHookGeneration(
        candidates=[_candidate(content_id, "A revised study cut latency by 30%")]
    )

    second = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert first is not None and second is not None
    assert second.hook.content_id != first.hook.content_id
    assert len(provider.attention_hook_calls) == 2
    async with session_factory() as session:
        hooks = list(
            (
                await session.scalars(
                    select(Content).where(Content.kind == ContentKind.ATTENTION_HOOK)
                )
            ).all()
        )
        assert len(hooks) == 2
        assert hooks[0].metadata_json["evidence_excerpt"] == "A study cut latency by 20%"
        assert hooks[1].metadata_json["evidence_excerpt"] == "A revised study cut latency by 30%"


async def test_generator_version_change_regenerates_lazily_and_keeps_old_rows(
    session_factory, monkeypatch
):
    """Treat generator version as an intentional refresh key without bulk cleanup."""
    user_id, item_id, _source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[_candidate(content_id, "A study cut latency by 20%", "Hook version one.")]
        )
    )
    service = AttentionHookService(session_factory, provider)
    first = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )
    monkeypatch.setattr(
        "app.services.attention_hooks.ATTENTION_HOOK_GENERATOR_VERSION",
        ATTENTION_HOOK_GENERATOR_VERSION + 1,
    )
    provider.attention_hook_generation = AttentionHookGeneration(
        candidates=[_candidate(content_id, "A study cut latency by 20%", "Hook version two.")]
    )

    second = await service.for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert first is not None and second is not None
    assert second.hook.content_id != first.hook.content_id
    assert second.hook.generator_version == ATTENTION_HOOK_GENERATOR_VERSION + 1
    assert len(provider.attention_hook_calls) == 2
    async with session_factory() as session:
        hooks = list(
            (
                await session.scalars(
                    select(Content).where(Content.kind == ContentKind.ATTENTION_HOOK)
                )
            ).all()
        )
        assert [hook.text for hook in hooks] == ["Hook version one.", "Hook version two."]


async def test_duplicate_hook_text_is_persisted_once(session_factory):
    """Deduplicate repeated phrasing even when the provider assigns different hook types."""
    user_id, item_id, _source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    repeated = "A study cut latency by 20%"
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(
            candidates=[
                AttentionHookCandidate(
                    hook_type=hook_type,
                    text=text,
                    evidence_excerpt=repeated,
                    source_content_id=content_id,
                )
                for hook_type, text in (
                    (AttentionHookType.PRACTICAL_VALUE, "Practical value"),
                    (AttentionHookType.QUESTION, " Practical   value "),
                    (AttentionHookType.CONTRAST, "A different grounded frame"),
                )
            ]
        )
    )

    result = await AttentionHookService(session_factory, provider).for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=1,
    )

    assert result is not None
    async with session_factory() as session:
        hooks = list(
            (
                await session.scalars(
                    select(Content).where(Content.kind == ContentKind.ATTENTION_HOOK)
                )
            ).all()
        )
        assert [hook.text for hook in hooks] == ["Practical value", "A different grounded frame"]


async def test_concurrent_generation_never_persists_more_than_three_hooks(session_factory):
    """Serialize hook writes after parallel provider calls to enforce the per-version cap."""
    user_id, item_id, _source_id, content_id, _claimed_at = await _seed_claimed_item(
        session_factory
    )
    candidates = [
        AttentionHookCandidate(
            hook_type=hook_type,
            text=f"Grounded hook {index}",
            evidence_excerpt="A study cut latency by 20%",
            source_content_id=content_id,
        )
        for index, hook_type in enumerate(
            (
                AttentionHookType.PRACTICAL_VALUE,
                AttentionHookType.QUESTION,
                AttentionHookType.CONTRAST,
            )
        )
    ]
    provider = FakeLlmProvider(
        attention_hook_generation=AttentionHookGeneration(candidates=candidates)
    )
    original = provider.generate_attention_hooks
    both_started = asyncio.Event()
    calls = 0

    async def wait_for_both(source_context: str, *, preferred_language: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return await original(source_context, preferred_language=preferred_language)

    provider.generate_attention_hooks = wait_for_both
    service = AttentionHookService(session_factory, provider)
    arguments = {
        "item_id": item_id,
        "user_id": user_id,
        "reminder_id": 1,
        "claim_generation": 1,
        "timeout_seconds": 3,
    }
    presentations = await asyncio.gather(
        service.for_reminder(**arguments), service.for_reminder(**arguments)
    )

    assert all(presentation is not None for presentation in presentations)
    async with session_factory() as session:
        hooks = list(
            (
                await session.scalars(
                    select(Content).where(
                        Content.item_id == item_id,
                        Content.kind == ContentKind.ATTENTION_HOOK,
                    )
                )
            ).all()
        )
        assert len(hooks) == 3


async def test_timeout_falls_back_without_resetting_claim_or_leaking_provider_task(
    session_factory,
):
    """Cancel timed-out generation and leave PM-08's claim timestamp authoritative."""
    user_id, item_id, _source_id, content_id, claimed_at = await _seed_claimed_item(session_factory)
    provider = FakeLlmProvider()
    cancelled = asyncio.Event()

    async def never_finishes(source_context: str, *, preferred_language: str):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.generate_attention_hooks = never_finishes
    result = await AttentionHookService(session_factory, provider).for_reminder(
        item_id=item_id,
        user_id=user_id,
        reminder_id=1,
        claim_generation=1,
        timeout_seconds=0.01,
    )

    assert result is None
    assert cancelled.is_set()
    async with session_factory() as session:
        reminder = await session.get(Reminder, 1)
        assert reminder.claimed_at == claimed_at
        assert reminder.status == "CLAIMED"
        assert "hook_content_id" not in reminder.payload_json
        assert "template_id" not in reminder.payload_json
