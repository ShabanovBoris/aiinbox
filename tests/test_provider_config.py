from app.config import Settings
from app.llm.transcription import OpenAiTranscriptionProvider, OpenRouterTranscriptionProvider
from app.main import build_provider, build_transcriber


def test_openrouter_analysis_uses_provider_specific_config():
    # Composition root должен переключать endpoint+credentials целиком, чтобы
    # OpenRouter не зависел от OPENAI_* и случайно не использовал production key.
    settings = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key="or-key",
        openrouter_analysis_model="google/test-analysis",
        openrouter_vision_model="google/test-vision",
    )

    provider = build_provider(settings)

    assert provider._model == "google/test-analysis"
    assert provider._vision_model == "google/test-vision"
    assert str(provider._client.base_url) == "https://openrouter.ai/api/v1/"


def test_openrouter_transcription_uses_same_compatible_endpoint():
    # STT остаётся тем же application contract и совместимым endpoint; отдельный
    # OpenRouter adapter добавляет только provider-specific segmentation limits.
    settings = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key="or-key",
        openrouter_transcription_model="openai/whisper-large-v3",
    )

    provider = build_transcriber(settings)

    assert isinstance(provider, OpenRouterTranscriptionProvider)
    assert provider._model == "openai/whisper-large-v3"
    assert str(provider._client.base_url) == "https://openrouter.ai/api/v1/"


def test_openai_composition_keeps_default_openai_endpoint():
    # Регрессия PR #17: optional base_url в общих adapters не должен менять
    # прежний OpenAI composition path.
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="openai-key",
        openai_analysis_model="gpt-test",
        openai_transcription_model="whisper-test",
        openai_vision_model="vision-test",
    )

    analysis = build_provider(settings)
    transcription = build_transcriber(settings)

    assert str(analysis._client.base_url) == "https://api.openai.com/v1/"
    assert type(transcription) is OpenAiTranscriptionProvider
    assert str(transcription._client.base_url) == "https://api.openai.com/v1/"
