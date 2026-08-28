"""Booking validation, listing, and cancellation (spec FR-5).

Conflict resolution itself lives in :mod:`app.services.preemption`; this module
owns everything that happens before it (steps 1-8 of §6.5) and everything that
happens to a booking afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.db.base import Connection, Database
from app.errors import (
    AppError,
    BEYOND_HORIZON,
    BOOKING_ALREADY_ENDED,
    BOOKING_NOT_CONFIRMED,
    BOOKING_NOT_FOUND,
    CROSSES_MIDNIGHT,
    END_NOT_AFTER_START,
    ForbiddenError,
    NOT_ACTIVE,
    NOT_BOOKING_OWNER,
    NotFoundError,
    OFF_GRID,
    OUTSIDE_WINDOW,
    QUOTA_EXCEEDED,
    ROOM_CLOSED,
    ROOM_INACTIVE,
    START_IN_PAST,
    TITLE_REQUIRED,
    TOO_LONG,
)
from app.models import (
    CANCELLED_BY_ADMIN,
    CANCELLED_BY_USER,
    CONFIRMED,
    Booking,
    Room,
    User,
)
from app.services import audit, closures
from app.settings import Settings
from app.timeutil import (
    ensure_utc,
    format_hhmm,
    is_aligned,
    local_date,
    minutes_between,
    minutes_since_midnight,
    now_utc,
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
class CancelResult:
    booking: Booking
    emails: list[EmailEvent] = field(default_factory=list)


def validate_request(
    conn: Connection,
    *,
    requester: User,
    room: Room,
    start_at: datetime,
    end_at: datetime,
    title: str,
    settings: Settings,
) -> None:
    """Run spec §6.5 steps 1-8 **in the order the spec gives**.

    The order is part of the contract: the acceptance tests assert on the
    *first* failure reported, so reordering these checks changes behaviour
    even though every individual rule is unchanged.

    A missing title is checked first as plain input validation; it is not one
    of the eight numbered rules, and neither is the room-closure check
    (7b), which the spec does not describe.
    """
    if not (title or "").strip():
        raise AppError(TITLE_REQUIRED)

    start_at = ensure_utc(start_at)
    end_at = ensure_utc(end_at)

    # 1. requester is an approved member
    if not requester.can_book:
        raise ForbiddenError(NOT_ACTIVE, {"status": requester.status})

    # 2. room is active
    if not room.is_active:
        raise AppError(ROOM_INACTIVE, {"room_id": room.id})

    # 3. slot alignment and ordering
    if end_at <= start_at:
        raise AppError(END_NOT_AFTER_START)
    slot = settings.slot_minutes
    if not is_aligned(start_at, slot) or not is_aligned(end_at, slot):
        raise AppError(OFF_GRID, {"slot_minutes": slot})

    # 4. maximum duration
    duration = minutes_between(start_at, end_at)
    if duration > settings.max_booking_minutes:
        raise AppError(
            TOO_LONG,
            {"max_minutes": settings.max_booking_minutes, "requested": duration},
        )

    # 5. start is in the future
    if start_at <= now_utc():
        raise AppError(START_IN_PAST)

    # 6. within the booking horizon
    horizon = settings.booking_horizon_days
    if local_date(start_at) > local_date(now_utc()) + timedelta(days=horizon):
        raise AppError(BEYOND_HORIZON, {"days": horizon})

    # 7. inside the room's open/close window, and not across midnight
    if local_date(start_at) != local_date(end_at - timedelta(microseconds=1)):
        raise AppError(CROSSES_MIDNIGHT)
    open_at, close_at = room.window(settings)
    start_minutes = minutes_since_midnight(start_at)
    # A booking ending exactly at local midnight is 0 minutes past midnight;
    # treat it as the end of the day rather than the start of the next one.
    end_minutes = minutes_since_midnight(end_at) or 24 * 60
    if start_minutes < open_at or end_minutes > close_at:
        raise AppError(
            OUTSIDE_WINDOW,
            {"open_time": format_hhmm(open_at), "close_time": format_hhmm(close_at)},
        )

    # 7b. an admin has closed this room for part of this day. Not one of the
    #     eight numbered rules -- the spec is silent on closures -- but the
    #     same kind as step 7 and placed with it: both answer "the room is not
    #     bookable then", while step 8 answers "you have booked too much".
    #     Reporting the quota for a slot nobody can have would be the wrong
    #     first message and would leak that the request was otherwise fine.
    #
    #     It must come after the midnight check: a closure is defined against
    #     one Taipei calendar date, so a booking spanning midnight has no
    #     single date to test against.
    #
    #     start_minutes/end_minutes are reused from step 7 rather than
    #     recomputed, which keeps the 24:00 end-of-day normalisation above.
    if not requester.is_admin:
        # Admins may book over a closure. That is the owner's explicit
        # decision, and it is an exemption from a §6.5 validation rule -- not
        # the preemption privilege CLAUDE.md rules out, which is about who
        # wins a contested slot in §7 and still depends on level alone. The
        # test keys on is_admin and never on level: a level-10 member is
        # blocked here, and an admin's level buys them nothing in §7.
        closed = closures.closure_at(
            conn, room.id, local_date(start_at), start_minutes, end_minutes
        )
        if closed is not None:
            raise AppError(
                ROOM_CLOSED,
                {
                    "date": local_date(start_at).isoformat(),
                    "start_time": format_hhmm(closed.start_minutes),
                    "end_time": format_hhmm(closed.end_minutes),
                    "reason": closed.reason or "",
                },
            )

    # 8. per-level quota on future confirmed bookings
    quota = settings.quota_for(requester.level)
    if quota is not None and future_confirmed_count(conn, requester.id) >= quota:
        raise AppError(QUOTA_EXCEEDED, {"quota": quota, "level": requester.level})


def future_confirmed_count(conn: Connection, user_id: str) -> int:
    """How many confirmed bookings the member still has ahead of them.

    "Future" means not yet finished, so a meeting in progress still counts
    against the quota.
    """
    return int(
        conn.query_value(
            "SELECT COUNT(*) FROM bookings"
            " WHERE user_id = ? AND status = ? AND end_at > ?",
            (user_id, CONFIRMED, now_utc()),
        )
        or 0
    )


def get_booking(conn: Connection, booking_id: str) -> Booking:
    row = conn.query_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))
    if row is None:
        raise NotFoundError(BOOKING_NOT_FOUND)
    return Booking.from_row(row)


def cancel_booking(db: Database, *, actor: User, booking_id: str) -> CancelResult:
    """Cancel a booking.

    An owner may cancel their own confirmed booking any time before it ends and
    receives E6. An admin may cancel anyone's, which sends the owner E5 instead
    because they did not ask for it (spec §6.5, §9.1).
    """

    def work(conn: Connection) -> tuple[Booking, str, dict[str, Any]]:
        booking = get_booking(conn, booking_id)
        is_owner = booking.user_id == actor.id
        if not is_owner and not actor.is_admin:
            raise ForbiddenError(NOT_BOOKING_OWNER)
        if booking.status != CONFIRMED:
            raise AppError(BOOKING_NOT_CONFIRMED, {"status": booking.status})
        if booking.end_at <= now_utc():
            raise AppError(BOOKING_ALREADY_ENDED)

        # An admin cancelling their own booking is an ordinary self-cancel.
        new_status = CANCELLED_BY_USER if is_owner else CANCELLED_BY_ADMIN
        now = now_utc()
        conn.execute(
            "UPDATE bookings SET status = ?, cancelled_at = ?, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (new_status, now, now, booking_id, CONFIRMED),
        )
        if not is_owner:
            audit.record(
                conn,
                actor_id=actor.id,
                action=audit.BOOKING_CANCELLED_BY_ADMIN,
                target_type="booking",
                target_id=booking_id,
                detail={"owner_id": booking.user_id},
            )
        owner = conn.query_one(
            "SELECT email, full_name FROM users WHERE id = ?", (booking.user_id,)
        )
        room = conn.query_one(
            "SELECT name FROM rooms WHERE id = ?", (booking.room_id,)
        )
        return get_booking(conn, booking_id), new_status, {
            "owner_email": (owner or {}).get("email"),
            "owner_name": (owner or {}).get("full_name", ""),
            "room_name": (room or {}).get("name"),
        }

    booking, new_status, extra = db.run_in_transaction(work)

    emails: list[EmailEvent] = []
    if extra["owner_email"]:
        emails.append(
            EmailEvent(
                kind="E6" if new_status == CANCELLED_BY_USER else "E5",
                to_email=extra["owner_email"],
                context={
                    "full_name": extra["owner_name"],
                    "reason": "self_cancelled"
                    if new_status == CANCELLED_BY_USER
                    else "cancelled_by_admin",
                    "room_name": extra["room_name"],
                    "start_at": booking.start_at,
                    "end_at": booking.end_at,
                    "title": booking.title,
                },
                related_booking_id=booking.id,
            )
        )
    return CancelResult(booking=booking, emails=emails)


def list_for_user(
    db: Database, user_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(upcoming, past)`` for the "my bookings" screen.

    ``past`` deliberately includes cancelled and preempted rows so a displaced
    member can see what happened to them and why (spec §8).
    """

    def work(conn: Connection) -> tuple[list[dict], list[dict]]:
        rows = conn.query_all(
            "SELECT b.*, r.name AS room_name FROM bookings b"
            " JOIN rooms r ON r.id = b.room_id"
            " WHERE b.user_id = ? ORDER BY b.start_at DESC",
            (user_id,),
        )
        now = now_utc()
        upcoming, past = [], []
        for row in rows:
            entry = dict(row)
            if row["status"] == CONFIRMED and row["end_at"] > now:
                upcoming.append(entry)
            else:
                past.append(entry)
        upcoming.sort(key=lambda entry: entry["start_at"])
        return upcoming, past

    return db.run_in_transaction(work)


def list_all(
    db: Database,
    *,
    room_id: str | None = None,
    user_id: str | None = None,
    day: Any = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Admin view of every booking, with optional filters (spec §6.6)."""

    def work(conn: Connection) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if room_id:
            clauses.append("b.room_id = ?")
            params.append(room_id)
        if user_id:
            clauses.append("b.user_id = ?")
            params.append(user_id)
        if day is not None:
            from app.timeutil import taipei_midnight

            start = taipei_midnight(day)
            clauses.append("b.start_at < ? AND b.end_at > ?")
            params.extend([start + timedelta(days=1), start])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return conn.query_all(
            "SELECT b.*, r.name AS room_name, u.full_name AS owner_name,"
            " u.department AS owner_department, u.email AS owner_email"
            " FROM bookings b JOIN rooms r ON r.id = b.room_id"
            " JOIN users u ON u.id = b.user_id"
            f"{where} ORDER BY b.start_at DESC LIMIT ?",
            tuple(params),
        )

    return db.run_in_transaction(work)
