"""Session management (spec FR-2, CONTRACT.md §5 Task 1).

A session is identified to the browser by an opaque cookie value. Only the
SHA-256 hash of that value is ever persisted (mirroring how email tokens are
stored, see :mod:`app.security`) so that a stolen database dump does not hand
out working session cookies.

The cookie itself -- name ``session``, flags ``HttpOnly``, ``SameSite=Lax``,
and ``Secure`` whenever the request is HTTPS -- is set by the web layer
(Task 5's ``app/web/`` pages); this module only mints and validates the
value.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app import security
from app.models import User
from app.timeutil import now_utc

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a hard import cycle
    from app.db.base import Database

#: Not one of the spec §5 admin-tunable settings, so a plain module constant
#: is appropriate here (see CONTRACT.md §3 convention 2).
SESSION_LIFETIME_DAYS = 30

#: Name of the cookie the web layer must set.
COOKIE_NAME = "session"


def create_session(db: "Database", user: User) -> tuple[str, datetime]:
    """Start a session for ``user``. Returns ``(raw cookie value, expires_at)``.

    Only the hash of the raw value is stored; the raw value is returned once
    and never persisted, exactly like an email token (spec §4.2).
    """
    raw, hashed = security.new_session_id()
    now = now_utc()
    expires_at = now + timedelta(days=SESSION_LIFETIME_DAYS)

    def work(conn) -> None:
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (hashed, user.id, now, expires_at),
        )

    db.run_in_transaction(work)
    return raw, expires_at


def resolve_session(db: "Database", raw_cookie: str | None) -> User | None:
    """The session's owner, or ``None`` if the cookie is missing, unknown,
    revoked, or expired.

    Never raises: an invalid session is simply "not logged in" to the web
    layer, not an error condition.
    """
    if not raw_cookie:
        return None
    hashed = security.hash_token(raw_cookie)

    def work(conn) -> User | None:
        session_row = conn.query_one("SELECT * FROM sessions WHERE id = ?", (hashed,))
        if session_row is None:
            return None
        if session_row["revoked_at"] is not None:
            return None
        if session_row["expires_at"] <= now_utc():
            return None
        user_row = conn.query_one(
            "SELECT * FROM users WHERE id = ?", (session_row["user_id"],)
        )
        return User.from_row(user_row) if user_row is not None else None

    return db.run_in_transaction(work)


def revoke_session(db: "Database", raw_cookie: str | None) -> None:
    """Log out one session (e.g. explicit logout). A no-op for an unknown
    or already-revoked cookie."""
    if not raw_cookie:
        return
    hashed = security.hash_token(raw_cookie)

    def work(conn) -> None:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now_utc(), hashed),
        )

    db.run_in_transaction(work)


def revoke_all_for_user(db: "Database", user_id: str) -> int:
    """Revoke every live session for ``user_id``. Returns how many were revoked.

    Used by password reset (spec §12 A7): a reset must invalidate every
    session an attacker (or the legitimate owner on another device) may be
    holding.
    """

    def work(conn) -> int:
        cursor = conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now_utc(), user_id),
        )
        return cursor.rowcount

    return db.run_in_transaction(work)


__all__ = [
    "COOKIE_NAME",
    "SESSION_LIFETIME_DAYS",
    "create_session",
    "resolve_session",
    "revoke_session",
    "revoke_all_for_user",
]
