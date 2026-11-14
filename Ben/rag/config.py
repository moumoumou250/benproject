from pathlib import Path
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from Ben.config.settings import _BenSettings, _BASE_CONFIG


class RagSettings(_BenSettings):
    model_config = SettingsConfigDict(**{**_BASE_CONFIG, "env_prefix": "BEN_RAG_"})
    knowledge_dir: Path = Path(__file__).parent / "knowledge_base"
    database: Path = Path(__file__).parent / "vector_store" / "knowledge.sqlite3"
    embedding_provider: str = "local"
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str = Field(default="", repr=False)
    embedding_base_url: str | None = None
    chunk_size: int = Field(default=1000, ge=100, le=10000)
    chunk_overlap: int = Field(default=150, ge=0)
