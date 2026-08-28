"""Room closures: shutting part of a day rather than a whole room (FR-5).

``rooms.is_active`` is all-or-nothing, which cannot say "meeting room 1 is
unavailable on 31 August between 12:00 and 15:00", nor "every weekday from
08:00 to 10:00 for the next six weeks". A closure is a rule, not a list of
days: one row covers a date range, so a six-week cleaning slot is a single
thing to read, edit and delete rather than forty-two of them.

The spec is silent on closures -- §4 already carries a per-room open/close
window, and this is the same kind of rule with a date attached -- so this
follows CLAUDE.md's "anything the spec does not specify is the implementer's
choice" rather than contradicting anything. It is not §13's "recurring
bookings", which is a member-facing feature about repeating *reservations*.

Two conventions this module exists to keep consistent:

* **Overlap is half-open**, via :func:`overlaps`. A booking that ends exactly
  when a closure begins does not overlap it, matching the rule
  ``preemption._overlapping`` uses for instants. The same function answers the
  question in both directions -- "may this booking go here?" and "which
  bookings are in the way of this closure?" -- because writing it twice is how
  the two ends drift apart, and the drift shows up as a booking that blocks a
  closure it would not itself have been blocked by.
* **Dates and weekdays are Taipei-local and computed in Python.** The two
  database dialects disagree on the weekday origin, and ``strftime``'s ``%``
  would be doubled on its way to Postgres by ``Connection._prepare``. The
  timezone conversion lives in :mod:`app.timeutil`, which CONTRACT.md §3 makes
  the only place allowed to do it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app import models
from app.errors import (
    AppError,
    CONFIRMATION_REQUIRED,
    ForbiddenError,
    MISSING_FIELD,
    NOT_ADMIN,
    NotFoundError,
)
from app.models import CANCELLED_BY_ADMIN, CONFIRMED, Booking, new_id
from app.services import audit
from app.timeutil import (
    format_hhmm,
    local_date,
    minutes_since_midnight,
    now_utc,
    parse_hhmm,
    taipei_midnight,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.base import Connection, Database
    from app.models import User

try:  # Task 2 owns the mailer; mirror accounts.py's tolerance of its absence.
    from app.services.mailer import EmailEvent
except ImportError:  # pragma: no cover

    @dataclass
    class EmailEvent:  # type: ignore[no-redef]
        kind: str
        to_email: str
        context: dict
        related_booking_id: str | None = None
        dedupe_key: str | None = None


#: Monday = bit 0, matching :meth:`datetime.date.weekday`, which is also what
#: ``timeutil.format_date_zh`` indexes its weekday names by.
ALL_WEEKDAYS = 0b1111111

#: A reason is shown to members on the day grid, so it has to fit in a slot
#: row. ``maxlength`` on the input only constrains a browser, not a POST.
MAX_REASON_LENGTH = 200

CLOSURE_NOT_FOUND = "CLOSURE_NOT_FOUND"


@dataclass(frozen=True)
class Closure:
    id: str
    room_id: str
    from_date: date
    to_date: date
    start_minutes: int
    end_minutes: int
    weekday_mask: int
    reason: str | None
    created_by: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Closure":
        return cls(
            id=row["id"],
            from_date=date.fromisoformat(str(row["from_date"])),
            to_date=date.fromisoformat(str(row["to_date"])),
            room_id=row["room_id"],
            start_minutes=int(row["start_minutes"]),
            end_minutes=int(row["end_minutes"]),
            weekday_mask=int(row["weekday_mask"]),
            reason=row["reason"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def covers_day(self, day: date) -> bool:
        return (
            self.from_date <= day <= self.to_date
            and bool(self.weekday_mask & (1 << day.weekday()))
        )

    @property
    def weekdays(self) -> list[int]:
        return [d for d in range(7) if self.weekday_mask & (1 << d)]

    @property
    def is_every_day(self) -> bool:
        return self.weekday_mask == ALL_WEEKDAYS


@dataclass
class ClosureResult:
    """Shaped like ``rooms.DeactivationResult``: the caller enqueues the mail."""

    closures: list[Closure] = field(default_factory=list)
    cancelled: list[Booking] = field(default_factory=list)
    emails: list[EmailEvent] = field(default_factory=list)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open overlap of two minute spans within one day.

    Two spans that merely touch -- one ending exactly where the next begins --
    do not overlap, which is the rule ``preemption._overlapping`` applies to
    booking instants.
    """
    return a_start < b_end and a_end > b_start


def _require_admin(actor: "User") -> None:
    if not actor.is_admin:
        raise ForbiddenError(NOT_ADMIN)


def _mask_from(weekdays: Any) -> int:
    if isinstance(weekdays, int):
        return weekdays
    mask = 0
    for day in weekdays:
        mask |= 1 << int(day)
    return mask


# --- reads -------------------------------------------------------------------


def for_day(conn: "Connection", room_id: str, day: date) -> list[Closure]:
    """Closures shutting part of ``day`` for one room, in display order.

    The ``id`` tiebreak matters: closures may overlap each other, and both the
    grid and the error message name only the first match. Without a total
    order Postgres could name a different reason on each page load.
    """
    iso = day.isoformat()
    rows = conn.query_all(
        "SELECT * FROM room_closures"
        " WHERE room_id = ? AND from_date <= ? AND to_date >= ?"
        " ORDER BY from_date, start_minutes, id",
        (room_id, iso, iso),
    )
    bit = 1 << day.weekday()
    return [
        Closure.from_row(row) for row in rows if int(row["weekday_mask"]) & bit
    ]


def closure_at(
    conn: "Connection",
    room_id: str,
    day: date,
    start_minutes: int,
    end_minutes: int,
) -> Closure | None:
    """The first closure overlapping this span, or ``None``."""
    for closure in for_day(conn, room_id, day):
        if overlaps(start_minutes, end_minutes, closure.start_minutes, closure.end_minutes):
            return closure
    return None


def list_closures(
    db: "Database", *, room_id: str | None = None, include_past: bool = False
) -> list[Closure]:
    today = local_date(now_utc()).isoformat()

    def work(conn: "Connection") -> list[Closure]:
        sql = "SELECT * FROM room_closures"
        where: list[str] = []
        params: list[Any] = []
        if room_id:
            where.append("room_id = ?")
            params.append(room_id)
        if not include_past:
            # Finished closures are hidden, never deleted: they are the record
            # of why a past day looks shut.
            where.append("to_date >= ?")
            params.append(today)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY from_date, start_minutes, id"
        return [Closure.from_row(row) for row in conn.query_all(sql, tuple(params))]

    return db.run_in_transaction(work)


def conflicting_bookings(
    conn: "Connection",
    *,
    room_id: str,
    from_date: date,
    to_date: date,
    start_minutes: int,
    end_minutes: int,
    weekday_mask: int,
) -> list[dict[str, Any]]:
    """Confirmed future bookings a closure would sit on top of.

    Narrowed in SQL to the closure's whole date span, then filtered in Python
    per candidate, because the weekday and the minutes-past-Taipei-midnight
    both need a timezone-aware extraction the two dialects disagree about.
    Filtering by booking rather than expanding the rule into one interval per
    day also means a rule spanning years costs no more than a rule spanning a
    week.

    ``end_at > now`` is load-bearing, not an optimisation. A closure may
    legitimately start in the past -- "15 Aug to 1 Oct" typed in September is
    the owner's own example -- and a finished booking inside it can never be
    cancelled (``cancel_booking`` refuses with ``BOOKING_ALREADY_ENDED``).
    Counting those as conflicts would deadlock the closure permanently with no
    way out of the screen.
    """
    span_start = taipei_midnight(from_date)
    span_end = taipei_midnight(to_date + timedelta(days=1))
    rows = conn.query_all(
        "SELECT b.*, u.full_name AS owner_name, u.department AS owner_department"
        " FROM bookings b JOIN users u ON u.id = b.user_id"
        " WHERE b.room_id = ? AND b.status = ?"
        "   AND b.start_at < ? AND b.end_at > ? AND b.end_at > ?"
        " ORDER BY b.start_at",
        (room_id, CONFIRMED, span_end, span_start, now_utc()),
    )

    found: list[dict[str, Any]] = []
    for row in rows:
        # The Taipei date, never row["start_at"].date(): an 07:00 Taipei
        # booking is 23:00 UTC the day before, and the UTC date would test the
        # wrong day's rule.
        day = local_date(row["start_at"])
        if not (1 << day.weekday()) & weekday_mask:
            continue
        booking_start = minutes_since_midnight(row["start_at"])
        booking_end = minutes_since_midnight(row["end_at"]) or 24 * 60
        if overlaps(start_minutes, end_minutes, booking_start, booking_end):
            found.append(row)
    return found


# --- writes ------------------------------------------------------------------


def _validate(
    *,
    from_date: date,
    to_date: date,
    start_minutes: int,
    end_minutes: int,
    weekday_mask: int,
    reason: str,
) -> None:
    if to_date < from_date:
        raise AppError(MISSING_FIELD, {"field": "to_date", "reason": "before_from_date"})
    if end_minutes <= start_minutes:
        raise AppError(
            MISSING_FIELD, {"field": "end_time", "reason": "must_be_after_start"}
        )
    if not weekday_mask:
        raise AppError(MISSING_FIELD, {"field": "weekdays", "reason": "none_selected"})
    if len(reason) > MAX_REASON_LENGTH:
        raise AppError(MISSING_FIELD, {"field": "reason", "reason": "too_long"})

    # A rule matching no date at all closes nothing, forever, silently. Only
    # spans shorter than a week can miss: any mask hits every weekday given
    # seven consecutive days.
    span = (to_date - from_date).days + 1
    if span < 7:
        days = (from_date + timedelta(days=i) for i in range(span))
        if not any(weekday_mask & (1 << d.weekday()) for d in days):
            raise AppError(
                MISSING_FIELD, {"field": "weekdays", "reason": "matches_no_dates"}
            )


def create_closure(
    db: "Database",
    actor: "User",
    *,
    room_ids: list[str],
    from_date: str,
    to_date: str,
    start_time: str,
    end_time: str,
    weekdays: Any = ALL_WEEKDAYS,
    reason: str = "",
    cancel_bookings: bool = False,
) -> ClosureResult:
    """Close part of a day for one or more rooms.

    Refuses with ``CONFIRMATION_REQUIRED`` when confirmed future bookings fall
    inside the range, carrying the list of them so the caller can show what is
    in the way. ``cancel_bookings=True`` is the second pass: it cancels those
    bookings and creates the closures in one transaction, so a member cannot
    slip a new booking into the gap between the two steps.
    """
    _require_admin(actor)
    if not room_ids:
        raise AppError(MISSING_FIELD, {"field": "room_id"})

    try:
        start_minutes = parse_hhmm(start_time)
        end_minutes = parse_hhmm(end_time)
    except ValueError:
        raise AppError(MISSING_FIELD, {"field": "start_time", "reason": "not_a_time"})
    try:
        start_day = date.fromisoformat(from_date.strip())
        # Blank "to" means a single day, which is the common case and the one
        # that should cost the fewest keystrokes.
        end_day = date.fromisoformat(to_date.strip()) if to_date.strip() else start_day
    except ValueError:
        raise AppError(MISSING_FIELD, {"field": "from_date", "reason": "not_a_date"})

    mask = _mask_from(weekdays)
    reason = reason.strip()
    _validate(
        from_date=start_day,
        to_date=end_day,
        start_minutes=start_minutes,
        end_minutes=end_minutes,
        weekday_mask=mask,
        reason=reason,
    )

    def work(conn: "Connection") -> tuple[list[Closure], list[dict[str, Any]]]:
        from app.services.rooms import get_room

        conflicts: list[dict[str, Any]] = []
        for room_id in room_ids:
            get_room(conn, room_id)  # raises ROOM_NOT_FOUND
            conflicts.extend(
                conflicting_bookings(
                    conn,
                    room_id=room_id,
                    from_date=start_day,
                    to_date=end_day,
                    start_minutes=start_minutes,
                    end_minutes=end_minutes,
                    weekday_mask=mask,
                )
            )

        if conflicts and not cancel_bookings:
            raise AppError(
                CONFIRMATION_REQUIRED,
                {
                    "future_bookings": len(conflicts),
                    "bookings": [
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "start_at": row["start_at"],
                            "end_at": row["end_at"],
                            "room_id": row["room_id"],
                            "owner_name": row["owner_name"],
                            "owner_department": row["owner_department"],
                        }
                        for row in conflicts
                    ],
                },
            )

        now = now_utc()
        for row in conflicts:
            conn.execute(
                "UPDATE bookings SET status = ?, cancelled_at = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (CANCELLED_BY_ADMIN, now, now, row["id"], CONFIRMED),
            )
            audit.record(
                conn,
                actor_id=actor.id,
                action=audit.BOOKING_CANCELLED_BY_ADMIN,
                target_type="booking",
                target_id=row["id"],
                detail={"reason": "room_closed", "room_id": row["room_id"]},
            )

        created: list[Closure] = []
        for room_id in room_ids:
            closure_id = new_id()
            conn.execute(
                "INSERT INTO room_closures (id, room_id, from_date, to_date,"
                " start_minutes, end_minutes, weekday_mask, reason, created_by,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    closure_id,
                    room_id,
                    start_day.isoformat(),
                    end_day.isoformat(),
                    start_minutes,
                    end_minutes,
                    mask,
                    reason or None,
                    actor.id,
                    now,
                ),
            )
            audit.record(
                conn,
                actor_id=actor.id,
                action=audit.ROOM_CLOSURE_CREATED,
                target_type="room",
                target_id=room_id,
                detail={
                    "from_date": start_day.isoformat(),
                    "to_date": end_day.isoformat(),
                    "start_time": format_hhmm(start_minutes),
                    "end_time": format_hhmm(end_minutes),
                    "weekday_mask": mask,
                    "cancelled_bookings": len(conflicts),
                },
            )
            created.append(
                Closure.from_row(
                    conn.query_one(
                        "SELECT * FROM room_closures WHERE id = ?", (closure_id,)
                    )
                )
            )
        return created, conflicts

    created, conflicts = db.run_in_transaction(work)

    # Mail only after the transaction commits (CONTRACT.md §3 rule 6): work()
    # may be retried, so it must have no effect outside the database.
    emails: list[EmailEvent] = []
    cancelled: list[Booking] = []
    if conflicts:
        rooms_by_id = _room_names(db, {row["room_id"] for row in conflicts})
        owners = _owner_emails(db, {row["user_id"] for row in conflicts})
        for row in conflicts:
            cancelled.append(Booking.from_row(row))
            address = owners.get(row["user_id"])
            if not address:
                continue
            emails.append(
                EmailEvent(
                    kind="E5",
                    to_email=address,
                    context={
                        "full_name": row["owner_name"],
                        "reason": "room_closed",
                        "room_name": rooms_by_id.get(row["room_id"], ""),
                        "title": row["title"],
                        "start_at": row["start_at"],
                        "end_at": row["end_at"],
                    },
                    related_booking_id=row["id"],
                )
            )
    return ClosureResult(closures=created, cancelled=cancelled, emails=emails)


def delete_closure(db: "Database", actor: "User", closure_id: str) -> None:
    _require_admin(actor)

    def work(conn: "Connection") -> None:
        row = conn.query_one("SELECT * FROM room_closures WHERE id = ?", (closure_id,))
        if row is None:
            raise NotFoundError(CLOSURE_NOT_FOUND)
        conn.execute("DELETE FROM room_closures WHERE id = ?", (closure_id,))
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.ROOM_CLOSURE_DELETED,
            target_type="room",
            target_id=row["room_id"],
            detail={
                "from_date": str(row["from_date"]),
                "to_date": str(row["to_date"]),
                "start_time": format_hhmm(int(row["start_minutes"])),
                "end_time": format_hhmm(int(row["end_minutes"])),
            },
        )

    db.run_in_transaction(work)


def delete_for_room(conn: "Connection", room_id: str) -> int:
    """Drop a room's closures. Used when the room itself is being deleted.

    They are configuration for a room that is ceasing to exist and nothing
    references them, so unlike bookings they can simply go.
    """
    rows = conn.query_all("SELECT id FROM room_closures WHERE room_id = ?", (room_id,))
    if rows:
        conn.execute("DELETE FROM room_closures WHERE room_id = ?", (room_id,))
    return len(rows)


def _room_names(db: "Database", room_ids: set[str]) -> dict[str, str]:
    if not room_ids:
        return {}

    def work(conn: "Connection") -> dict[str, str]:
        placeholders = ", ".join("?" for _ in room_ids)
        rows = conn.query_all(
            f"SELECT id, name FROM rooms WHERE id IN ({placeholders})",
            tuple(room_ids),
        )
        return {row["id"]: row["name"] for row in rows}

    return db.run_in_transaction(work)


def _owner_emails(db: "Database", user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}

    def work(conn: "Connection") -> dict[str, str]:
        placeholders = ", ".join("?" for _ in user_ids)
        rows = conn.query_all(
            f"SELECT id, email FROM users WHERE id IN ({placeholders})",
            tuple(user_ids),
        )
        return {row["id"]: row["email"] for row in rows}

    return db.run_in_transaction(work)


__all__ = [
    "ALL_WEEKDAYS",
    "CLOSURE_NOT_FOUND",
    "MAX_REASON_LENGTH",
    "Closure",
    "ClosureResult",
    "closure_at",
    "conflicting_bookings",
    "create_closure",
    "delete_closure",
    "delete_for_room",
    "for_day",
    "list_closures",
    "overlaps",
]
