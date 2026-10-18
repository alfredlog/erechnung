"""Settings come from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "E-Rechnung")
    kosit_url: str | None = os.getenv("KOSIT_URL") or None   # e.g. http://kosit:8080/
    database_url: str | None = os.getenv("DATABASE_URL") or None
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "15"))


settings = Settings()
