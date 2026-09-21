from app.config import Settings
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
    # STT остаётся отдельным application contract, но OpenRouter реализует тот же
    # OpenAI-compatible /audio/transcriptions API, поэтому новый adapter не нужен.
    settings = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key="or-key",
        openrouter_transcription_model="openai/whisper-large-v3",
    )

    provider = build_transcriber(settings)

    assert provider._model == "openai/whisper-large-v3"
    assert str(provider._client.base_url) == "https://openrouter.ai/api/v1/"
