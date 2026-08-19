"""Application configuration."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """App settings loaded from environment / .env file."""

    OLLAMA_MODEL: str = "llama3.1:latest"
    OLLAMA_EMBEDDING_MODEL: str = "nomic-embed-text:latest"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    CHROMA_COLLECTION: str = "corporate_filings"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
