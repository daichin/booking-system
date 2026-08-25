"""Database backend selection.

``DATABASE_URL`` chooses the backend, mirroring how the app is configured in
production (spec §10.2). Anything Postgres-shaped uses psycopg; a ``sqlite://``
URL or an empty value falls back to SQLite for local development and tests.
"""

from __future__ import annotations

from app.db.base import (
    POSTGRES,
    SQLITE,
    Connection,
    Database,
    DatabaseError,
    PostgresDatabase,
    SqliteDatabase,
    execute_script,
)

_POSTGRES_SCHEMES = ("postgres://", "postgresql://")


def create_database(url: str | None) -> Database:
    """Build the backend named by a ``DATABASE_URL``-style string."""
    if url and url.startswith(_POSTGRES_SCHEMES):
        return PostgresDatabase(url)
    if not url or url == "sqlite://:memory:":
        return SqliteDatabase(":memory:")
    if url.startswith("sqlite://"):
        return SqliteDatabase(url[len("sqlite://") :])
    raise DatabaseError(f"unsupported DATABASE_URL scheme: {url.split(':', 1)[0]!r}")


__all__ = [
    "POSTGRES",
    "SQLITE",
    "Connection",
    "Database",
    "DatabaseError",
    "PostgresDatabase",
    "SqliteDatabase",
    "create_database",
    "execute_script",
]
