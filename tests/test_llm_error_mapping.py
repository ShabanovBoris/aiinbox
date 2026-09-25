import logging
from types import SimpleNamespace

import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent, UserProfile
from app.errors import AppError
from app.llm.base import LlmError
from app.llm.openai import OpenAiProvider, _map_provider_error
from app.llm.transcription import OpenAiTranscriptionProvider

PRIVATE_MARKER = "PRIVATE_PROVIDER_PAYLOAD_7f1a"


def _status_error(error_type, status_code):
    response = SimpleNamespace(
        request=object(),
        status_code=status_code,
        headers={"x-request-id": "safe-request-id"},
    )
    return error_type(PRIVATE_MARKER, response=response, body={"message": PRIVATE_MARKER})


@pytest.mark.parametrize(
    ("error", "expected_code", "permanent"),
    [
        (APITimeoutError(request=object()), "LLM_TIMEOUT", False),
        (_status_error(RateLimitError, 429), "LLM_RATE_LIMITED", False),
        (_status_error(AuthenticationError, 401), "LLM_AUTH_FAILED", True),
        (_status_error(PermissionDeniedError, 403), "LLM_AUTH_FAILED", True),
        (_status_error(BadRequestError, 400), "LLM_CONFIG_FAILED", True),
        (
            APIConnectionError(message=PRIVATE_MARKER, request=object()),
            "LLM_FAILED",
            False,
        ),
        (_status_error(APIStatusError, 503), "LLM_FAILED", False),
        (RuntimeError(PRIVATE_MARKER), "LLM_FAILED", True),
    ],
)
def test_provider_errors_map_to_safe_bounded_codes(error, expected_code, permanent, caplog):
    with caplog.at_level(logging.WARNING, logger="app.llm.openai"):
        mapped = _map_provider_error(
            error, operation="answer_inbox", provider="openrouter", model="test-model"
        )
    assert mapped.code == expected_code
    assert mapped.permanent is permanent
    assert PRIVATE_MARKER not in str(mapped)
    assert PRIVATE_MARKER not in caplog.text
    assert "operation=answer_inbox" in caplog.text
    assert "provider=openrouter" in caplog.text
    assert "exception_type=" in caplog.text


class _RaisingCompletionClient:
    def __init__(self, error):
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_request):
        raise self.error


class _StaticCompletionClient:
    def __init__(self, content):
        self.content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_request):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


@pytest.mark.parametrize(
    "operation",
    [
        "analyze",
        "summarize_chunk",
        "generate_attention_hooks",
        "answer_inbox",
        "profile_update",
        "describe_images",
    ],
)
async def test_provider_operations_suppress_private_sdk_traceback(operation, caplog):
    provider = OpenAiProvider("unused", "test-model", vision_model="test-vision")
    provider._client = _RaisingCompletionClient(RuntimeError(PRIVATE_MARKER))
    content = NormalizedContent(source_type=SourceType.TEXT, text=PRIVATE_MARKER)
    profile = UserProfile(free_text=PRIVATE_MARKER)

    async def run_operation():
        if operation == "analyze":
            await provider.analyze(content, profile, [])
        elif operation == "summarize_chunk":
            await provider.summarize_chunk(PRIVATE_MARKER)
        elif operation == "generate_attention_hooks":
            await provider.generate_attention_hooks(PRIVATE_MARKER, preferred_language="ru")
        elif operation == "answer_inbox":
            await provider.answer_inbox(PRIVATE_MARKER, PRIVATE_MARKER, preferred_language="ru")
        elif operation == "profile_update":
            await provider.profile_update(PRIVATE_MARKER, profile)
        else:
            await provider.describe_images([], PRIVATE_MARKER, preferred_language="ru")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(LlmError) as caught:
            await run_operation()
        error = caught.value
        assert error.code == "LLM_FAILED"
        assert error.__cause__ is None
        assert error.__suppress_context__ is True
        try:
            raise error
        except LlmError:
            logging.getLogger("test.provider.traceback").exception("worker traceback test")

    assert PRIVATE_MARKER not in str(error)
    assert PRIVATE_MARKER not in caplog.text


async def test_profile_schema_error_does_not_embed_private_model_output():
    provider = OpenAiProvider("unused", "test-model")
    provider._client = _StaticCompletionClient(PRIVATE_MARKER)

    with pytest.raises(LlmError) as caught:
        await provider.profile_update("private instruction", UserProfile())

    assert caught.value.code == "INVALID_LLM_OUTPUT"
    assert PRIVATE_MARKER not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (APITimeoutError(request=object()), "TIMEOUT"),
        (APIConnectionError(message=PRIVATE_MARKER, request=object()), "TRANSCRIPTION_FAILED"),
    ],
)
async def test_transcription_provider_error_text_and_cause_are_suppressed(
    tmp_path, exception, expected_code
):
    audio_path = tmp_path / "input.wav"
    audio_path.write_bytes(b"audio")

    async def create(**_request):
        raise exception

    provider = OpenAiTranscriptionProvider("unused", "test-model")
    provider._client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
    )

    with pytest.raises(AppError) as caught:
        await provider.transcribe(audio_path)

    assert caught.value.code == expected_code
    assert PRIVATE_MARKER not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
