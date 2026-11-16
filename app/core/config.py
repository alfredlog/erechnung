"""Settings come from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _flag(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "E-Rechnung"))
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./data/dev.db"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data/files")))
    kosit_url: str | None = field(default_factory=lambda: _env("KOSIT_URL") or None)
    max_upload_mb: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_MB", "15")))
    cookie_secure: bool = field(default_factory=lambda: _flag("COOKIE_SECURE", False))
    allow_registration: bool = field(default_factory=lambda: _flag("ALLOW_REGISTRATION", True))
    # Process uploads directly in the web request (dev/tests) instead of the background worker.
    inline_jobs: bool = field(default_factory=lambda: _flag("INLINE_JOBS", False))
    # LLM for reading PDF/scan invoices. Any OpenAI-compatible API; default: Mistral (EU-hosted).
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.mistral.ai/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "mistral-small-latest"))
    # Domain for the customers' receiving addresses (MX record → this server). Empty = feature off.
    inbound_domain: str = field(default_factory=lambda: _env("INBOUND_DOMAIN").strip().lower())
    mailin_port: int = field(default_factory=lambda: int(_env("MAILIN_PORT", "2525")))
    max_mail_mb: int = field(default_factory=lambda: int(_env("MAX_MAIL_MB", "25")))
    # Sending invoices by e-mail (e.g. IONOS: smtp.ionos.de, 587, starttls). Empty host = feature off.
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_out_port: int = field(default_factory=lambda: int(_env("SMTP_OUT_PORT", "587")))
    smtp_security: str = field(default_factory=lambda: _env("SMTP_SECURITY", "starttls"))  # starttls/ssl/none
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM"))
    max_mails_per_day: int = field(default_factory=lambda: int(_env("MAX_MAILS_PER_DAY", "50")))
    imap_poll_minutes: int = field(default_factory=lambda: int(_env("IMAP_POLL_MINUTES", "10")))

    # Operator of the service (Impressum, Datenschutz, AGB, AVV). Kept in .env, not in the code.
    operator_name: str = field(default_factory=lambda: _env("OPERATOR_NAME"))
    operator_street: str = field(default_factory=lambda: _env("OPERATOR_STREET"))
    operator_city: str = field(default_factory=lambda: _env("OPERATOR_CITY"))
    operator_email: str = field(default_factory=lambda: _env("OPERATOR_EMAIL"))
    operator_phone: str = field(default_factory=lambda: _env("OPERATOR_PHONE"))
    operator_small_business: bool = field(default_factory=lambda: _flag("OPERATOR_SMALL_BUSINESS", True))

    @property
    def sending_enabled(self) -> bool:
        return bool(self.smtp_host and self.mail_from)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


settings = Settings()
