from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Gooaye Tracker"
    postgres_dsn: str = "postgresql+psycopg://gooaye:change-me@localhost:5432/gooaye_tracker"
    # Optional: a separate, pre-existing multi-market security master database
    # (TW/US/JP/KR company name <-> ticker) used only as a read-only reference
    # for resolving company names to tickers. Leave blank to skip it and fall
    # back to the small seed list in config/settings.yaml alone.
    xstocktracker_db_url: str = ""

    llm_provider: str = "ollama"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:30b"
    llm_api_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_api_model: str = ""
    bai_api_url: str = "https://api.b.ai/v1"
    bai_api_key: str = ""
    bai_api_model: str = "glm-5.3-flash"
    # glm-5.3-flash always reasons; "low" keeps it from spending the whole
    # max_tokens budget on reasoning_content instead of the actual JSON reply.
    bai_reasoning_effort: str = "low"
    llm_timeout_seconds: float = 300

    whisper_model: str = "small"
    whisper_device: str = "cpu"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
