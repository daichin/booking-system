"""Audit trail (spec FR-7).

Preemption events, level changes, approvals and rejections, admin
cancellations, and room deactivations are kept forever and are admin-visible
and CSV-exportable.

:func:`record` must be called inside the same transaction as the change it
describes, so the trail cannot drift from what actually happened.
"""

from __future__ import annotations

import json
from typing import Any

from app.db.base import Connection
from app.models import new_id
from app.timeutil import now_utc

# Action names. Keep them stable: the admin UI and CSV export key off these.
LEVEL_CHANGED = "level_changed"
USER_APPROVED = "user_approved"
USER_REJECTED = "user_rejected"
USER_SUSPENDED = "user_suspended"
USER_REACTIVATED = "user_reactivated"
USER_DELETED = "user_deleted"
USER_INVITED = "user_invited"
INVITATION_REVOKED = "invitation_revoked"
BOOKING_CANCELLED_BY_ADMIN = "booking_cancelled_by_admin"
BOOKING_PREEMPTED = "booking_preempted"
ROOM_CREATED = "room_created"
ROOM_UPDATED = "room_updated"
ROOM_DEACTIVATED = "room_deactivated"
ROOM_REACTIVATED = "room_reactivated"
SETTING_CHANGED = "setting_changed"
PASSWORD_RESET = "password_reset"


def record(
    conn: Connection,
    *,
    actor_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Append one audit entry. Returns its id."""
    entry_id = new_id()
    conn.execute(
        "INSERT INTO audit_log (id, actor_user_id, action, target_type, target_id,"
        " detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            entry_id,
            actor_id,
            action,
            target_type,
            target_id,
            json.dumps(detail, ensure_ascii=False) if detail else None,
            now_utc(),
        ),
    )
    return entry_id


def recent(conn: Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    """Most recent entries, newest first, with the actor's name resolved."""
    rows = conn.query_all(
        "SELECT a.*, u.full_name AS actor_name FROM audit_log a"
        " LEFT JOIN users u ON u.id = a.actor_user_id"
        " ORDER BY a.created_at DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["detail"] = json.loads(row["detail"]) if row["detail"] else {}
    return rows
