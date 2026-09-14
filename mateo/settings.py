import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    database_url: str
    storage: Path
    mode: str = "local"
    mock: bool = True
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls):
        mode = os.getenv("MATEO_MODE", "local")
        if mode not in {"local", "apps_script", "production"}:
            raise ValueError("Invalid MATEO_MODE")
        url = os.getenv("DATABASE_URL", "sqlite:///local_storage/mateo.db")
        mock = os.getenv("GOOGLE_MOCK_MODE", "true").lower() == "true"
        if mode == "production" and (not url.startswith("postgresql+psycopg://") or mock):
            raise ValueError("Production requires PostgreSQL and real Google configuration")
        origins = tuple(filter(None, os.getenv("CORS_ORIGINS", "").split(",")))
        if "*" in origins:
            raise ValueError("Use explicit CORS origins")
        return cls(url, Path(os.getenv("LOCAL_STORAGE_PATH", "local_storage")), mode, mock, origins)
