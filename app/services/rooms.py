"""Room management and availability (spec FR-4).

Rooms are never hard-deleted while bookings reference them; they are
deactivated, which hides them from booking but preserves history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.db.base import Connection
from app.db.base import Database
from app.errors import (
    AppError,
    CONFIRMATION_REQUIRED,
    ForbiddenError,
    MISSING_FIELD,
    NOT_ADMIN,
    NotFoundError,
    ROOM_HAS_BOOKINGS,
    ROOM_NOT_FOUND,
)
from app.models import (
    CANCELLED_BY_ADMIN,
    CONFIRMED,
    Booking,
    Room,
    User,
    new_id,
)
from app.services import audit
from app.settings import Settings
from app.timeutil import (
    format_hhmm,
    now_utc,
    parse_hhmm,
    taipei_midnight,
)

try:  # Task 2 owns the mailer; fall back so this module imports standalone.
    from app.services.mailer import EmailEvent
except ImportError:  # pragma: no cover - only before Task 2 lands
    @dataclass
    class EmailEvent:  # type: ignore[no-redef]
        kind: str
        to_email: str
        context: dict
        related_booking_id: str | None = None
        dedupe_key: str | None = None


@dataclass
class DeactivationResult:
    room: Room
    cancelled: list[Booking] = field(default_factory=list)
    emails: list[EmailEvent] = field(default_factory=list)


@dataclass
class RoomDay:
    """One room's bookable window and confirmed bookings for a Taipei day."""

    room: Room
    open_minutes: int
    close_minutes: int
    bookings: list[dict[str, Any]] = field(default_factory=list)


def _require_admin(actor: User) -> None:
    if not actor.is_admin:
        raise ForbiddenError(NOT_ADMIN)


def get_room(conn: Connection, room_id: str) -> Room:
    row = conn.query_one("SELECT * FROM rooms WHERE id = ?", (room_id,))
    if row is None:
        raise NotFoundError(ROOM_NOT_FOUND)
    return Room.from_row(row)


def list_rooms(db: Database, *, include_inactive: bool = False) -> list[Room]:
    def work(conn: Connection) -> list[Room]:
        sql = "SELECT * FROM rooms"
        if not include_inactive:
            sql += " WHERE is_active = 1" if conn.dialect == "sqlite" else \
                   " WHERE is_active = TRUE"
        sql += " ORDER BY name"
        return [Room.from_row(row) for row in conn.query_all(sql)]

    return db.run_in_transaction(work)


def _clean_fields(fields: dict[str, Any], *, require_name: bool) -> dict[str, Any]:
    """Validate and normalise room attributes."""
    cleaned: dict[str, Any] = {}

    if "name" in fields or require_name:
        name = (fields.get("name") or "").strip()
        if not name:
            raise AppError(MISSING_FIELD, {"field": "name"})
        cleaned["name"] = name

    for key in ("location", "equipment_note"):
        if key in fields:
            value = fields.get(key)
            cleaned[key] = (value or "").strip() or None

    if "capacity" in fields:
        raw = fields.get("capacity")
        if raw in (None, ""):
            cleaned["capacity"] = None
        else:
            capacity = int(raw)
            if capacity <= 0:
                raise AppError(MISSING_FIELD, {"field": "capacity"})
            cleaned["capacity"] = capacity

    for key, target in (("open_time", "open_minutes"), ("close_time", "close_minutes")):
        if key in fields:
            raw = fields.get(key)
            cleaned[target] = None if raw in (None, "") else parse_hhmm(str(raw))
        elif target in fields:
            cleaned[target] = fields[target]

    open_at = cleaned.get("open_minutes")
    close_at = cleaned.get("close_minutes")
    if open_at is not None and close_at is not None and close_at <= open_at:
        raise AppError(
            MISSING_FIELD, {"field": "close_time", "reason": "must_be_after_open"}
        )
    return cleaned


def create_room(db: Database, actor: User, **fields: Any) -> Room:
    _require_admin(actor)
    cleaned = _clean_fields(fields, require_name=True)
    room_id = new_id()

    def work(conn: Connection) -> Room:
        now = now_utc()
        conn.execute(
            "INSERT INTO rooms (id, name, capacity, location, equipment_note,"
            " is_active, open_minutes, close_minutes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                cleaned["name"],
                cleaned.get("capacity"),
                cleaned.get("location"),
                cleaned.get("equipment_note"),
                True,
                cleaned.get("open_minutes"),
                cleaned.get("close_minutes"),
                now,
                now,
            ),
        )
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.ROOM_CREATED,
            target_type="room",
            target_id=room_id,
            detail={"name": cleaned["name"]},
        )
        return get_room(conn, room_id)

    return db.run_in_transaction(work)


def update_room(db: Database, actor: User, room_id: str, **fields: Any) -> Room:
    _require_admin(actor)
    cleaned = _clean_fields(fields, require_name=False)
    if not cleaned:
        return db.run_in_transaction(lambda conn: get_room(conn, room_id))

    def work(conn: Connection) -> Room:
        get_room(conn, room_id)  # raises if missing
        assignments = ", ".join(f"{column} = ?" for column in cleaned)
        conn.execute(
            f"UPDATE rooms SET {assignments}, updated_at = ? WHERE id = ?",
            (*cleaned.values(), now_utc(), room_id),
        )
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.ROOM_UPDATED,
            target_type="room",
            target_id=room_id,
            detail={key: str(value) for key, value in cleaned.items()},
        )
        return get_room(conn, room_id)

    return db.run_in_transaction(work)


def future_confirmed_bookings(conn: Connection, room_id: str) -> list[Booking]:
    rows = conn.query_all(
        "SELECT * FROM bookings WHERE room_id = ? AND status = ? AND end_at > ?"
        " ORDER BY start_at",
        (room_id, CONFIRMED, now_utc()),
    )
    return [Booking.from_row(row) for row in rows]


def set_active(
    db: Database,
    actor: User,
    room_id: str,
    active: bool,
    *,
    cancel_bookings: bool = False,
) -> DeactivationResult:
    """Deactivate or reactivate a room.

    Spec FR-4: deactivating a room that still has future confirmed bookings
    requires explicit confirmation and offers to cancel them, which sends E5
    to each owner. Without ``cancel_bookings`` the caller gets
    ``CONFIRMATION_REQUIRED`` listing what would be affected.
    """
    _require_admin(actor)

    def work(conn: Connection) -> tuple[Room, list[Booking]]:
        room = get_room(conn, room_id)
        affected: list[Booking] = []

        if not active:
            affected = future_confirmed_bookings(conn, room_id)
            if affected and not cancel_bookings:
                raise AppError(
                    CONFIRMATION_REQUIRED,
                    {
                        "room_id": room_id,
                        "future_bookings": len(affected),
                    },
                )
            now = now_utc()
            for booking in affected:
                conn.execute(
                    "UPDATE bookings SET status = ?, cancelled_at = ?, updated_at = ?"
                    " WHERE id = ?",
                    (CANCELLED_BY_ADMIN, now, now, booking.id),
                )
                audit.record(
                    conn,
                    actor_id=actor.id,
                    action=audit.BOOKING_CANCELLED_BY_ADMIN,
                    target_type="booking",
                    target_id=booking.id,
                    detail={"reason": "room_deactivated", "room_id": room_id},
                )

        conn.execute(
            "UPDATE rooms SET is_active = ?, updated_at = ? WHERE id = ?",
            (active, now_utc(), room_id),
        )
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.ROOM_REACTIVATED if active else audit.ROOM_DEACTIVATED,
            target_type="room",
            target_id=room_id,
            detail={"cancelled_bookings": len(affected)},
        )
        return get_room(conn, room_id), affected

    room, cancelled = db.run_in_transaction(work)

    # Email only after the transaction commits (CONTRACT.md §3 rule 6).
    emails: list[EmailEvent] = []
    if cancelled:
        owners = _owners(db, [booking.user_id for booking in cancelled])
        for booking in cancelled:
            owner = owners.get(booking.user_id)
            if not owner:
                continue
            emails.append(
                EmailEvent(
                    kind="E5",
                    to_email=owner["email"],
                    context={
                        "full_name": owner["full_name"],
                        "reason": "room_deactivated",
                        "room_name": room.name,
                        "start_at": booking.start_at,
                        "end_at": booking.end_at,
                        "title": booking.title,
                    },
                    related_booking_id=booking.id,
                )
            )
    return DeactivationResult(room=room, cancelled=cancelled, emails=emails)


def _owners(db: Database, user_ids: list[str]) -> dict[str, dict[str, str]]:
    """Resolve owner contact details for post-commit notifications."""
    if not user_ids:
        return {}
    unique = list(dict.fromkeys(user_ids))
    placeholders = ", ".join("?" for _ in unique)

    def work(conn: Connection) -> dict[str, dict[str, str]]:
        rows = conn.query_all(
            f"SELECT id, email, full_name FROM users WHERE id IN ({placeholders})",
            tuple(unique),
        )
        return {
            row["id"]: {"email": row["email"], "full_name": row["full_name"]}
            for row in rows
        }

    return db.run_in_transaction(work)


def delete_room(db: Database, actor: User, room_id: str) -> None:
    """Hard-delete a room, refused when any booking references it (FR-4)."""
    _require_admin(actor)

    def work(conn: Connection) -> None:
        get_room(conn, room_id)
        referencing = conn.query_value(
            "SELECT COUNT(*) FROM bookings WHERE room_id = ?", (room_id,)
        )
        if referencing:
            raise AppError(ROOM_HAS_BOOKINGS, {"bookings": int(referencing)})
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))

    db.run_in_transaction(work)


def availability(
    db: Database, *, day: date, room_ids: list[str] | None = None
) -> list[RoomDay]:
    """Confirmed bookings per room for one Taipei calendar day (spec §8).

    Only ``confirmed`` rows occupy a room, so cancelled and preempted history
    is excluded here even though it is retained forever.
    """
    day_start = taipei_midnight(day)
    day_end = day_start + timedelta(days=1)

    def work(conn: Connection) -> list[RoomDay]:
        settings = Settings.load(conn)
        sql = "SELECT * FROM rooms"
        params: list[Any] = []
        if room_ids:
            placeholders = ", ".join("?" for _ in room_ids)
            sql += f" WHERE id IN ({placeholders})"
            params.extend(room_ids)
        else:
            sql += " WHERE is_active = 1" if conn.dialect == "sqlite" else \
                   " WHERE is_active = TRUE"
        sql += " ORDER BY name"

        result: list[RoomDay] = []
        for row in conn.query_all(sql, tuple(params)):
            room = Room.from_row(row)
            open_at, close_at = room.window(settings)
            booking_rows = conn.query_all(
                "SELECT b.*, u.full_name AS owner_name, u.department AS owner_department"
                " FROM bookings b JOIN users u ON u.id = b.user_id"
                " WHERE b.room_id = ? AND b.status = ?"
                "   AND b.start_at < ? AND b.end_at > ?"
                " ORDER BY b.start_at",
                (room.id, CONFIRMED, day_end, day_start),
            )
            result.append(
                RoomDay(
                    room=room,
                    open_minutes=open_at,
                    close_minutes=close_at,
                    bookings=[_public_booking(row) for row in booking_rows],
                )
            )
        return result

    return db.run_in_transaction(work)


def _public_booking(row: dict[str, Any]) -> dict[str, Any]:
    """Booking projection safe for any logged-in member.

    Spec §7.2 and assumption 7: the title and the owner's name are visible to
    everyone logged in; the owner's email address never is.
    """
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "owner": {
            "full_name": row["owner_name"],
            "department": row["owner_department"],
        },
    }


def window_label(open_minutes: int, close_minutes: int) -> tuple[str, str]:
    return format_hhmm(open_minutes), format_hhmm(close_minutes)
