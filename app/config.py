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

    # Сменный LLM-провайдер: model ids только через конфиг (PRODUCT_SPEC §29).
    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_analysis_model: str = ""
    llm_timeout_seconds: int = 120

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        ids = self.allowed_telegram_user_ids.replace(" ", "")
        return frozenset(int(x) for x in ids.split(",") if x)

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids
