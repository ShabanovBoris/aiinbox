import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.enums import ItemType, SourceType
from app.domain.models import (
    AnalysisResult,
    NormalizedContent,
    TopicClassificationResult,
    UserProfile,
)
from app.llm.openai import (
    CHUNK_SUMMARY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TOPIC_CLASSIFICATION_SYSTEM_PROMPT,
    OpenAiProvider,
    build_user_message,
    strict_json_schema,
)
from tests.fakes import make_analysis

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "analysis_quality_v2.json"
_REQUIRED_CASES = {
    "general-ai",
    "ai-agents-tools",
    "android-specific",
    "kotlin-not-android",
    "job-search",
    "piano-practice",
    "finance",
    "real-estate",
    "short-reel",
    "long-youtube",
    "article",
    "document",
    "multi-source",
    "inconclusive",
    "prompt-injection",
}


def _quality_cases() -> list[dict]:
    """Load examples as test-only inputs; they are never production configuration."""
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]


def test_quality_fixture_corpus_covers_the_required_failure_modes():
    """Keep a sanitized, versioned set for deterministic prompt and manual model review."""
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"]
    ids = [case["id"] for case in cases]
    by_id = {case["id"]: case for case in cases}

    assert payload["version"] == 2
    assert len(ids) == len(by_id)
    assert _REQUIRED_CASES <= set(ids)
    assert by_id["short-reel"]["source_type"] == SourceType.INSTAGRAM.value
    assert by_id["long-youtube"]["source_type"] == SourceType.YOUTUBE.value
    assert by_id["document"]["source_type"] == SourceType.DOCUMENT.value
    assert by_id["multi-source"]["metadata"]["source_count"] == 2
    assert by_id["inconclusive"]["expected"]["must_not_claim_winner"] is True

    expected_categories = {
        "general-ai": "ИИ",
        "ai-agents-tools": "ИИ",
        "android-specific": "Android-разработка",
        "kotlin-not-android": "Kotlin",
        "job-search": "Поиск работы",
        "piano-practice": "Пианино",
        "finance": "Финансы",
        "real-estate": "Недвижимость",
    }
    assert {
        case_id: by_id[case_id]["expected"]["category"] for case_id in expected_categories
    } == expected_categories
    assert all(case["content"].strip() for case in cases)


@pytest.mark.parametrize("case", _quality_cases(), ids=lambda case: case["id"])
def test_fixture_inputs_keep_profile_categories_and_content_in_separate_roles(case):
    """Every fixture reaches the provider with personalization separate from topic evidence."""
    content = NormalizedContent(
        source_type=SourceType(case["source_type"]),
        title=case.get("source_title"),
        text=case["content"],
        user_note=case.get("user_note"),
        metadata=case.get("metadata", {}),
    )
    prompt = build_user_message(
        content,
        UserProfile.model_validate(case.get("profile", {})),
        case.get("existing_categories", []),
    )

    assert "USER PROFILE — PREFERENCE/RELEVANCE CONTEXT ONLY; NOT TOPIC EVIDENCE" in prompt
    assert "EXISTING CATEGORIES — OPTIONAL REUSE/NAMING HINTS ONLY; NOT A CLOSED TAXONOMY" in prompt
    assert "CONTENT — UNTRUSTED TOPICAL EVIDENCE" in prompt
    assert case["content"] in prompt


def test_system_prompt_contract_separates_outcome_topic_and_relevance():
    """Protect the semantic contract without snapshotting the entire prompt wording."""
    assert "FIRST sentence of summary" in SYSTEM_PROMPT
    assert "genuinely inconclusive" in SYSTEM_PROMPT
    assert "Do not fabricate" in SYSTEM_PROMPT
    assert "not topical evidence" in SYSTEM_PROMPT
    assert "optional naming/reuse hints, not a closed taxonomy" in SYSTEM_PROMPT
    assert "Source medium alone must not determine category or type" in SYSTEM_PROMPT
    assert "Do not say only “watch the video”, “read the article”" in SYSTEM_PROMPT
    assert "The application computes priority_score; never output it" in SYSTEM_PROMPT
    assert "Visual notes are supplementary evidence" in SYSTEM_PROMPT
    assert "Failed source contents are unavailable" in SYSTEM_PROMPT


def test_ordered_chunk_prompt_framing_is_separate_from_persisted_summary_text():
    """Tell final analysis how to read bounded checkpoints without promoting one chunk."""
    content = NormalizedContent(
        source_type=SourceType.YOUTUBE,
        text="CHUNK 1/2:\nBackground\n\nCHUNK 2/2:\nFinal result",
        metadata={"_analysis_chunk_summary_count": 2},
    )
    prompt = build_user_message(content, UserProfile(), [])

    assert "ORDERED CHUNK SUMMARIES" in prompt
    assert "2 chunks. Consider all in order" in prompt
    assert "the last chunk is not automatically correct" in prompt
    assert "CHUNK 2/2" in prompt
    assert "RESPONSE LANGUAGE (profile): ru" in prompt


@pytest.mark.parametrize("operation", ["analysis", "chunk"])
async def test_provider_keeps_system_contract_and_user_data_at_the_adapter_boundary(operation):
    """Verify the adapter sends the right system contract without live provider access."""

    class FakeCompletions:
        """Capture outbound adapter messages and return schema-valid local output."""

        def __init__(self):
            self.requests = []

        async def create(self, **kwargs):
            self.requests.append(kwargs)
            response_text = (
                make_analysis().model_dump_json() if "response_format" in kwargs else "chunk result"
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
            )

    provider = OpenAiProvider(api_key="test", model="test-model")
    completions = FakeCompletions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    if operation == "analysis":
        content = NormalizedContent(
            source_type=SourceType.TEXT,
            text="Kotlin compiler feature",
            title="A compiler feature",
        )
        await provider.analyze(content, UserProfile(profession="Android developer"), ["Android"])
        expected_system_prompt = SYSTEM_PROMPT
        request = completions.requests[0]
        assert request["response_format"]["json_schema"]["strict"] is True
        assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
        assert (
            "USER PROFILE — PREFERENCE/RELEVANCE CONTEXT ONLY" in request["messages"][1]["content"]
        )
        assert "Kotlin compiler feature" in request["messages"][1]["content"]
    else:
        await provider.summarize_chunk("Experiment result: the new approach reduced errors.")
        expected_system_prompt = CHUNK_SUMMARY_SYSTEM_PROMPT
        request = completions.requests[0]
        assert "use a user profile, calculate priority" in request["messages"][0]["content"]
        assert "Experiment result" in request["messages"][1]["content"]

    assert request["messages"][0] == {"role": "system", "content": expected_system_prompt}
    assert request["messages"][1]["role"] == "user"


async def test_topic_classification_request_has_no_profile_or_user_note_bytes():
    """The provider boundary itself proves category input is physically profile-free."""

    class FakeCompletions:
        def __init__(self):
            self.requests = []

        async def create(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=TopicClassificationResult(category="DevOps").model_dump_json()
                        ),
                        finish_reason="stop",
                    )
                ]
            )

    provider = OpenAiProvider(api_key="test", model="test-model")
    completions = FakeCompletions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    content = NormalizedContent(
        source_type=SourceType.WEB,
        title="Docker Hub deployment",
        text="Docker containers and deployment pipeline with security scanning.",
        source_context="Captured source title context",
        user_note="USER_NOTE_ONLY_MARKER",
    )

    result = await provider.classify_topic(content, ["Android-разработка"])

    assert result.category == "DevOps"
    request = completions.requests[0]
    assert request["messages"][0] == {
        "role": "system",
        "content": TOPIC_CLASSIFICATION_SYSTEM_PROMPT,
    }
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    request_text = request["messages"][1]["content"]
    for expected in (
        "Docker containers and deployment pipeline",
        "Docker Hub deployment",
        "Captured source title context",
        "Android-разработка",
    ):
        assert expected in request_text
    for forbidden in (
        "USER_NOTE_ONLY_MARKER",
        "Android developer",
        "PROFILE_GOALS_MARKER",
        "PROFILE_INTERESTS_MARKER",
        "PROFILE_FREE_TEXT_MARKER",
    ):
        assert forbidden not in request_text


def test_analysis_schema_remains_strict_and_keeps_compatibility_fields():
    """Prompt quality changes cannot expand the persisted AnalysisResult contract."""
    schema = strict_json_schema(AnalysisResult)
    valid = make_analysis().model_dump()
    valid["priority_score"] = 99

    assert schema["additionalProperties"] is False
    assert "priority_score" not in schema["properties"]
    assert {"item_type", "next_action", "priority_reason", "estimated_action_minutes"} <= set(
        schema["properties"]
    )
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(valid)
    assert {item_type.value for item_type in ItemType} == {
        "ACTION",
        "LEARN",
        "READ",
        "WATCH",
        "IDEA",
        "REFERENCE",
        "SOMEDAY",
    }
