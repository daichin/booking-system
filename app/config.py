"""Deployment configuration read from the environment (spec §10.2).

Secret names are fixed by the spec and must match SETUP.md exactly, because a
non-technical owner pastes them into the GitHub Secrets screen by name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Secrets the deploy workflow must supply. The workflow fails loudly and
#: names any that are missing (spec §12 E2).
REQUIRED_SECRETS = (
    "DATABASE_URL",
    "EMAIL_PROVIDER_API_KEY",
    "EMAIL_FROM_ADDRESS",
    "APP_BASE_URL",
    "SESSION_SECRET",
    "ADMIN_EMAIL",
    "ADMIN_INITIAL_PASSWORD",
    "CRON_SECRET",
)


@dataclass(frozen=True)
class Config:
    database_url: str = ""
    email_api_key: str = ""
    email_from: str = ""
    email_from_name: str = "會議室預約系統"
    base_url: str = "http://localhost:8000"
    session_secret: str = ""
    admin_email: str = ""
    admin_initial_password: str = ""
    cron_secret: str = ""
    port: int = 8000
    #: Transport name: "brevo" in production, "fake" in tests and local dev.
    email_transport: str = "fake"
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_production(self) -> bool:
        return self.email_transport != "fake"


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (defaults to ``os.environ``)."""
    source = os.environ if env is None else env
    missing = tuple(name for name in REQUIRED_SECRETS if not source.get(name))
    transport = source.get("EMAIL_TRANSPORT") or (
        "brevo" if source.get("EMAIL_PROVIDER_API_KEY") else "fake"
    )
    return Config(
        database_url=source.get("DATABASE_URL", ""),
        email_api_key=source.get("EMAIL_PROVIDER_API_KEY", ""),
        email_from=source.get("EMAIL_FROM_ADDRESS", ""),
        email_from_name=source.get("EMAIL_FROM_NAME", "會議室預約系統"),
        base_url=source.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        session_secret=source.get("SESSION_SECRET", ""),
        admin_email=source.get("ADMIN_EMAIL", "").strip().lower(),
        admin_initial_password=source.get("ADMIN_INITIAL_PASSWORD", ""),
        cron_secret=source.get("CRON_SECRET", ""),
        port=int(source.get("PORT", "8000")),
        email_transport=transport,
        missing=missing,
    )
