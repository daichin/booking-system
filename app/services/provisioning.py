"""First-run seeding (spec §10.1 item 5).

Creates the initial administrator from deploy secrets and a few example rooms.
Everything here is idempotent, because the deploy workflow runs it on every
deploy and re-running must not duplicate anything (spec §12 E4).
"""

from __future__ import annotations

from typing import Any

from app.config import Config
from app.db.base import Connection, Database
from app.models import ACTIVE, MAX_LEVEL, new_id
from app.security import hash_password, normalise_email
from app.services import audit
from app.timeutil import now_utc

#: Rooms created on a brand-new installation so the owner sees a working
#: system immediately. Only seeded when the room table is completely empty, so
#: an admin who deletes them does not get them back on the next deploy.
EXAMPLE_ROOMS = (
    {"name": "第一會議室", "capacity": 12, "location": "3 樓",
     "equipment_note": "投影機、白板"},
    {"name": "第二會議室", "capacity": 6, "location": "3 樓",
     "equipment_note": "電視螢幕"},
    {"name": "大型會議室", "capacity": 30, "location": "5 樓",
     "equipment_note": "投影機、視訊設備、白板"},
)


def seed_initial_data(db: Database, config: Config) -> dict[str, Any]:
    """Create the first admin and the example rooms if they are missing."""

    def work(conn: Connection) -> dict[str, Any]:
        return {
            "admin": _ensure_admin(conn, config),
            "rooms_seeded": _ensure_rooms(conn),
        }

    return db.run_in_transaction(work)


def _ensure_admin(conn: Connection, config: Config) -> str:
    """Create the administrator named by ``ADMIN_EMAIL``.

    Spec §10.3: the account must be forced to change its password at first
    login, because the initial password is sitting in a GitHub secret. An
    existing account is never overwritten -- if the owner has already changed
    the password, redeploying must not reset it.
    """
    if not config.admin_email or not config.admin_initial_password:
        return "skipped_no_secrets"

    email = normalise_email(config.admin_email)
    existing = conn.query_one("SELECT id, is_admin FROM users WHERE email = ?", (email,))
    if existing is not None:
        if not existing["is_admin"]:
            # The address was registered as an ordinary member first; promote
            # it rather than leaving the deployment with no administrator.
            conn.execute(
                "UPDATE users SET is_admin = ?, status = ?, updated_at = ?"
                " WHERE id = ?",
                (True, ACTIVE, now_utc(), existing["id"]),
            )
            return "promoted"
        return "exists"

    user_id = new_id()
    now = now_utc()
    conn.execute(
        "INSERT INTO users (id, email, password_hash, full_name, department, phone,"
        " level, status, is_admin, must_change_password, email_verified_at,"
        " approved_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            email,
            hash_password(config.admin_initial_password),
            "系統管理員",
            "管理",
            "-",
            MAX_LEVEL,
            ACTIVE,
            True,
            True,      # forced password change at first login
            now,
            now,
            now,
            now,
        ),
    )
    audit.record(
        conn,
        actor_id=None,
        action="admin_provisioned",
        target_type="user",
        target_id=user_id,
        detail={"email": email},
    )
    return "created"


def _ensure_rooms(conn: Connection) -> int:
    if conn.query_value("SELECT COUNT(*) FROM rooms"):
        return 0
    now = now_utc()
    for room in EXAMPLE_ROOMS:
        conn.execute(
            "INSERT INTO rooms (id, name, capacity, location, equipment_note,"
            " is_active, open_minutes, close_minutes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                room["name"],
                room["capacity"],
                room["location"],
                room["equipment_note"],
                True,
                None,   # inherit the global window from settings
                None,
                now,
                now,
            ),
        )
    return len(EXAMPLE_ROOMS)
