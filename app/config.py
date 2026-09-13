from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Единственная точка конфигурации приложения; секреты только через окружение."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    processing_concurrency: int = 2
    processing_poll_seconds: float = 1.0
    default_timezone: str = "UTC"

    # Web extraction (Phase 3)
    min_extracted_text_length: int = 300
    web_timeout_seconds: float = 30.0
    max_download_bytes: int = 5_000_000
    max_redirects: int = 5
    web_max_attempts: int = 3
    web_backoff_seconds: float = 0.5
    # Playwright fallback жёстко отключён (нет SSRF-safe browser boundary);
    # вернётся отдельным изменением с pinned/proxied browser network boundary.

    # Voice/audio (Phase 5)
    temp_dir: str = "./temp"
    max_audio_bytes: int = 20_000_000  # Telegram bot API отдаёт файлы до 20 MB
    transcription_timeout_seconds: float = 120.0

    # YouTube (Phase 6)
    youtube_max_duration_seconds: int = 7200
    youtube_max_audio_bytes: int = 50_000_000
    subtitle_langs: str = "ru,en"

    # Сменный LLM-провайдер: model ids только через конфиг (PRODUCT_SPEC §29).
    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_analysis_model: str = ""
    openai_transcription_model: str = ""
    llm_timeout_seconds: int = 120

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        ids = self.allowed_telegram_user_ids.replace(" ", "")
        return frozenset(int(x) for x in ids.split(",") if x)

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids
