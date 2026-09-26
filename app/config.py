from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Единственная точка конфигурации приложения; секреты только через окружение."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    # Backups live outside the canonical DB path and rotation is bounded.
    backup_dir: str = "./backups"
    backup_keep: int = Field(14, ge=1)
    # User-owned export artifacts have their own persistent location and bounded retention.
    export_dir: str = "./exports"
    export_retention_seconds: int = Field(86_400, ge=60)
    max_export_content_chars: int = Field(10_000_000, ge=1)
    processing_concurrency: int = Field(2, ge=1)
    processing_poll_seconds: float = Field(1.0, gt=0)
    reminder_poll_seconds: float = Field(30.0, gt=0)
    processing_timeout_seconds: float = Field(900.0, gt=0)
    shutdown_timeout_seconds: float = Field(30.0, gt=0)
    default_timezone: str = "UTC"

    # Web extraction (Phase 3)
    min_extracted_text_length: int = Field(300, ge=1)
    web_timeout_seconds: float = Field(30.0, gt=0)
    max_download_bytes: int = Field(5_000_000, ge=1)
    max_redirects: int = Field(5, ge=1)
    web_max_attempts: int = Field(3, ge=1)
    web_backoff_seconds: float = Field(0.5, ge=0)
    # Playwright fallback жёстко отключён (нет SSRF-safe browser boundary);
    # вернётся отдельным изменением с pinned/proxied browser network boundary.

    # Profile seed (Phase 8): если файл существует, пустые профили получают seed
    profile_seed_file: str = "profile.yaml"

    # Voice/audio (Phase 5)
    temp_dir: str = "./temp"
    max_audio_bytes: int = Field(20_000_000, ge=1)  # Telegram bot API отдаёт файлы до 20 MB
    transcription_timeout_seconds: float = Field(120.0, gt=0)

    # YouTube (Phase 6)
    youtube_max_duration_seconds: int = Field(7200, ge=1)
    youtube_max_audio_bytes: int = Field(50_000_000, ge=1)
    youtube_max_video_bytes: int = Field(50_000_000, ge=1)
    youtube_max_subtitle_bytes: int = Field(2_000_000, ge=1)
    youtube_download_timeout_seconds: float = Field(300.0, gt=0)
    subtitle_langs: str = "ru,en"

    # Instagram Reel extraction uses the same yt-dlp boundary with independently
    # tunable remote-media limits and an optional operator-provided cookie file.
    instagram_max_duration_seconds: int = Field(7200, ge=1)
    instagram_max_audio_bytes: int = Field(50_000_000, ge=1)
    instagram_max_video_bytes: int = Field(50_000_000, ge=1)
    instagram_cookies_file: str = ""

    # Video visual analysis (Phase 7)
    max_video_bytes: int = Field(20_000_000, ge=1)
    max_video_duration_seconds: int = Field(7200, ge=1)
    video_frame_interval_seconds: int = Field(20, ge=1)
    video_max_frames: int = Field(120, ge=1)
    video_scene_threshold: float = Field(0.35, ge=0.0, le=1.0)

    # Bounded document acquisition and durable text extraction (PM-03).
    max_document_bytes: int = Field(20_000_000, ge=1)
    max_document_text_chars: int = Field(500_000, ge=1)

    # Сменный LLM-провайдер: model ids только через конфиг (PRODUCT_SPEC §29).
    llm_provider: Literal["openai", "openrouter"] = "openai"
    openai_api_key: str = ""
    openai_analysis_model: str = ""
    openai_transcription_model: str = ""
    openai_vision_model: str = ""
    # OpenRouter использует OpenAI-compatible API, но отдельные credentials/model ids
    # не дают случайно отправить production OpenAI-трафик через другой provider.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_analysis_model: str = ""
    openrouter_transcription_model: str = ""
    openrouter_vision_model: str = ""
    llm_timeout_seconds: int = Field(120, gt=0)
    llm_chunk_size_chars: int = Field(12_000, gt=0, validation_alias="CONTENT_CHUNK_MAX_CHARS")
    llm_chunk_overlap_chars: int = Field(0, ge=0, validation_alias="CONTENT_CHUNK_OVERLAP_CHARS")

    @field_validator("default_timezone")
    @classmethod
    def validate_default_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown DEFAULT_TIMEZONE: {value}") from exc
        return value

    @field_validator("allowed_telegram_user_ids")
    @classmethod
    def validate_allowed_user_ids(cls, value: str) -> str:
        ids = value.replace(" ", "")
        try:
            parsed = [int(item) for item in ids.split(",") if item]
        except ValueError as exc:
            raise ValueError("ALLOWED_TELEGRAM_USER_IDS must be comma-separated integers") from exc
        if any(user_id <= 0 for user_id in parsed):
            raise ValueError("ALLOWED_TELEGRAM_USER_IDS must contain positive integers")
        return value

    @model_validator(mode="after")
    def validate_chunk_bounds(self) -> "Settings":
        if self.llm_chunk_overlap_chars >= self.llm_chunk_size_chars:
            raise ValueError(
                "CONTENT_CHUNK_OVERLAP_CHARS must be smaller than CONTENT_CHUNK_MAX_CHARS"
            )
        return self

    @model_validator(mode="after")
    def validate_export_directory(self) -> "Settings":
        """Keep portable user archives physically separate from operational backups."""
        export_path = Path(self.export_dir).resolve()
        backup_path = Path(self.backup_dir).resolve()
        if (
            export_path == backup_path
            or export_path.is_relative_to(backup_path)
            or backup_path.is_relative_to(export_path)
        ):
            raise ValueError("EXPORT_DIR and BACKUP_DIR must use separate directories")
        return self

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        ids = self.allowed_telegram_user_ids.replace(" ", "")
        return frozenset(int(x) for x in ids.split(",") if x)

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids
