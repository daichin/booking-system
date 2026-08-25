"""The preemption engine (spec §7).

This is the single place where a booking is created. Everything about
conflict resolution lives here, in one transaction-safe function, because the
spec calls it the highest-risk part of the system.

The rules, restated because each one is easy to get subtly wrong:

* Only a **strictly higher current level** may preempt. Equal level never
  wins, and ``level_at_booking`` is an audit snapshot that must never be used
  for the decision -- the victim's *current* level is what counts (§12 C8).
* The protection window is measured against the **victim's** ``start_at``:
  a booking is immune once ``now >= start_at - preemption_protection_minutes``.
  This also makes an already-started booking permanently immune (§12 C7).
* Any overlap at all cancels the victim's booking **in its entirety** -- no
  splitting, no trimming (§12 C4).
* **All-or-nothing**: if any single overlap is not preemptible, the whole
  request is refused and nothing changes (§12 C5).
* Preempting yourself is never possible; it is reported as ``SELF_OVERLAP``.
* Being an admin grants no privilege here; only ``level`` matters.
* Email is enqueued strictly **after** the transaction commits, so a rollback
  can never announce a cancellation that did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.db.base import Connection, Database
from app.errors import (
    AVAILABLE,
    BLOCKED,
    CREATED,
    EQUAL_OR_HIGHER_LEVEL,
    NotFoundError,
    PREEMPTION_REQUIRED,
    PROTECTED_WINDOW,
    SELF_OVERLAP,
    USER_NOT_FOUND,
)
from app.models import CONFIRMED, PREEMPTED, Booking, Room, User, new_id
from app.services import audit
from app.services.bookings import validate_request
from app.services.rooms import get_room
from app.settings import Settings
from app.timeutil import ensure_utc, now_utc

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
class Victim:
    """A booking that would be (or was) displaced."""

    booking: Booking
    owner_view: dict[str, Any]      # public_view(): name, department, level
    owner_email: str                # for E5 only; never sent to the requester
    room_name: str = ""

    def for_client(self) -> dict[str, Any]:
        """Projection safe to hand the requester's browser.

        Spec §7.2: show name and department, never the email address.
        """
        return {
            "booking_id": self.booking.id,
            "room_name": self.room_name,
            "start_at": self.booking.start_at.isoformat(),
            "end_at": self.booking.end_at.isoformat(),
            "title": self.booking.title,
            "owner": {
                "full_name": self.owner_view["full_name"],
                "department": self.owner_view["department"],
            },
        }


@dataclass
class BookingAttempt:
    outcome: str
    booking: Booking | None = None
    victims: list[Victim] = field(default_factory=list)
    reason: str | None = None
    blocker: dict[str, Any] | None = None
    emails: list[EmailEvent] = field(default_factory=list)

    @property
    def created(self) -> bool:
        return self.outcome == CREATED

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"outcome": self.outcome}
        if self.booking is not None:
            payload["booking"] = {
                "id": self.booking.id,
                "room_id": self.booking.room_id,
                "title": self.booking.title,
                "start_at": self.booking.start_at.isoformat(),
                "end_at": self.booking.end_at.isoformat(),
                "status": self.booking.status,
            }
        if self.victims:
            key = "displaced" if self.outcome == CREATED else "victims"
            payload[key] = [victim.for_client() for victim in self.victims]
        if self.reason:
            payload["reason"] = self.reason
        if self.blocker:
            payload["blocker"] = self.blocker
        return payload


class _Blocked(Exception):
    """Internal control flow: abandons the transaction with a reason."""

    def __init__(self, reason: str, blocker: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.blocker = blocker


class _NeedsConfirmation(Exception):
    """Internal control flow: all overlaps are preemptible but unconfirmed."""

    def __init__(self, victims: list[Victim]) -> None:
        super().__init__(PREEMPTION_REQUIRED)
        self.victims = victims


def _load_user(conn: Connection, user_id: str) -> User:
    row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if row is None:
        raise NotFoundError(USER_NOT_FOUND)
    return User.from_row(row)


def _overlapping(
    conn: Connection, room_id: str, start_at: datetime, end_at: datetime
) -> list[Booking]:
    """Confirmed bookings overlapping the requested span, locked for update.

    The half-open comparison (``start < end`` and ``end > start``) means two
    bookings that merely touch -- one ending exactly when the next begins --
    do not overlap.
    """
    rows = conn.query_all(
        "SELECT * FROM bookings"
        " WHERE room_id = ? AND status = ? AND start_at < ? AND end_at > ?"
        " ORDER BY start_at" + conn.for_update(),
        (room_id, CONFIRMED, end_at, start_at),
    )
    return [Booking.from_row(row) for row in rows]


def _blocker_view(booking: Booking, owner: User, room: Room) -> dict[str, Any]:
    return {
        "booking_id": booking.id,
        "room_name": room.name,
        "start_at": booking.start_at.isoformat(),
        "end_at": booking.end_at.isoformat(),
        "owner": {"full_name": owner.full_name, "department": owner.department},
    }


def attempt_booking(
    db: Database,
    *,
    requester_id: str,
    room_id: str,
    start_at: datetime,
    end_at: datetime,
    title: str,
    confirm_preemption: bool = False,
    dry_run: bool = False,
) -> BookingAttempt:
    """Create a booking, resolving conflicts per spec §7.

    ``dry_run=True`` is **phase 1** of §7.2: it writes nothing and returns
    ``AVAILABLE``, ``PREEMPTION_REQUIRED`` (with the victim list), or
    ``BLOCKED`` (with the first blocker's reason).

    ``dry_run=False`` is **phase 2**. It re-runs the entire check inside the
    transaction; the phase-1 result is never trusted, because the world can
    change between the two calls.
    """
    start_at = ensure_utc(start_at)
    end_at = ensure_utc(end_at)

    def work(conn: Connection) -> BookingAttempt:
        settings = Settings.load(conn)
        requester = _load_user(conn, requester_id)
        room = get_room(conn, room_id)

        # Steps 1-8 of §6.5. Raises AppError on failure, which rolls back.
        validate_request(
            conn,
            requester=requester,
            room=room,
            start_at=start_at,
            end_at=end_at,
            title=title,
            settings=settings,
        )

        overlaps = _overlapping(conn, room_id, start_at, end_at)

        if not overlaps:
            if dry_run:
                return BookingAttempt(outcome=AVAILABLE)
            booking = _insert_booking(
                conn, requester, room_id, start_at, end_at, title
            )
            return BookingAttempt(outcome=CREATED, booking=booking)

        victims = _classify_overlaps(conn, requester, room, overlaps, settings)

        # Reaching here means every overlap is individually preemptible.
        if dry_run or not confirm_preemption:
            raise _NeedsConfirmation(victims)

        booking = _insert_booking(conn, requester, room_id, start_at, end_at, title)
        _displace(conn, booking, requester, victims)
        return BookingAttempt(outcome=CREATED, booking=booking, victims=victims)

    try:
        attempt = db.run_in_transaction(work)
    except _Blocked as blocked:
        return BookingAttempt(
            outcome=BLOCKED, reason=blocked.reason, blocker=blocked.blocker
        )
    except _NeedsConfirmation as needed:
        return BookingAttempt(outcome=PREEMPTION_REQUIRED, victims=needed.victims)

    # --- everything below happens only after a successful COMMIT ------------
    if attempt.outcome == CREATED and attempt.booking is not None:
        attempt.emails = _emails_for(db, attempt)
        _enqueue(db, attempt.emails)
    return attempt


def _classify_overlaps(
    conn: Connection,
    requester: User,
    room: Room,
    overlaps: list[Booking],
    settings: Settings,
) -> list[Victim]:
    """Decide whether every overlap can be displaced. All-or-nothing.

    The first overlap that fails raises :class:`_Blocked`, which abandons the
    transaction -- so a request that is refused leaves absolutely nothing
    changed, including any earlier overlap in the list (§12 C5).
    """
    protection = timedelta(minutes=settings.preemption_protection_minutes)
    moment = now_utc()
    victims: list[Victim] = []

    for booking in overlaps:
        if booking.user_id == requester.id:
            raise _Blocked(
                SELF_OVERLAP,
                {
                    "booking_id": booking.id,
                    "room_name": room.name,
                    "start_at": booking.start_at.isoformat(),
                    "end_at": booking.end_at.isoformat(),
                },
            )

        owner = _load_user(conn, booking.user_id)

        # Current level, deliberately not booking.level_at_booking (§12 C8).
        if owner.level >= requester.level:
            raise _Blocked(
                EQUAL_OR_HIGHER_LEVEL, _blocker_view(booking, owner, room)
            )

        # Measured against the victim's start time, not the new booking's.
        if moment >= booking.start_at - protection:
            raise _Blocked(PROTECTED_WINDOW, _blocker_view(booking, owner, room))

        victims.append(
            Victim(
                booking=booking,
                owner_view=owner.public_view(),
                owner_email=owner.email,
                room_name=room.name,
            )
        )

    return victims


def _insert_booking(
    conn: Connection,
    requester: User,
    room_id: str,
    start_at: datetime,
    end_at: datetime,
    title: str,
) -> Booking:
    booking_id = new_id()
    now = now_utc()
    conn.execute(
        "INSERT INTO bookings (id, room_id, user_id, title, start_at, end_at,"
        " status, level_at_booking, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            booking_id,
            room_id,
            requester.id,
            title.strip(),
            start_at,
            end_at,
            CONFIRMED,
            requester.level,   # audit snapshot only
            now,
            now,
        ),
    )
    row = conn.query_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))
    assert row is not None
    return Booking.from_row(row)


def _displace(
    conn: Connection, winner: Booking, requester: User, victims: list[Victim]
) -> None:
    """Mark victims preempted, log it, and drop their pending reminders."""
    now = now_utc()
    for victim in victims:
        conn.execute(
            "UPDATE bookings SET status = ?, preempted_by_booking_id = ?,"
            " cancelled_at = ?, updated_at = ? WHERE id = ? AND status = ?",
            (PREEMPTED, winner.id, now, now, victim.booking.id, CONFIRMED),
        )
        conn.execute(
            "INSERT INTO preemption_log (id, victim_booking_id, winner_booking_id,"
            " victim_user_id, winner_user_id, victim_level, winner_level, room_id,"
            " occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                victim.booking.id,
                winner.id,
                victim.booking.user_id,
                requester.id,
                victim.owner_view["level"],
                requester.level,
                winner.room_id,
                now,
            ),
        )
        audit.record(
            conn,
            actor_id=requester.id,
            action=audit.BOOKING_PREEMPTED,
            target_type="booking",
            target_id=victim.booking.id,
            detail={
                "winner_booking_id": winner.id,
                "victim_level": victim.owner_view["level"],
                "winner_level": requester.level,
            },
        )
        # Spec §7.3: cancel the victim's pending reminder. Anything already
        # sent is left alone; only queued mail is withdrawn.
        conn.execute(
            "UPDATE email_log SET status = 'skipped',"
            " error = 'booking preempted' WHERE dedupe_key = ? AND status = 'queued'",
            (f"reminder:{victim.booking.id}",),
        )


def _emails_for(db: Database, attempt: BookingAttempt) -> list[EmailEvent]:
    """Build E4 for the winner and E5 for each displaced member (§9.1)."""
    booking = attempt.booking
    assert booking is not None

    def lookup(conn: Connection) -> tuple[str, str, str]:
        owner = conn.query_one(
            "SELECT email, full_name FROM users WHERE id = ?", (booking.user_id,)
        )
        room = conn.query_one("SELECT name FROM rooms WHERE id = ?", (booking.room_id,))
        return (
            (owner or {}).get("email", ""),
            (owner or {}).get("full_name", ""),
            (room or {}).get("name", ""),
        )

    winner_email, winner_name, room_name = db.run_in_transaction(lookup)

    events: list[EmailEvent] = []
    if winner_email:
        events.append(
            EmailEvent(
                kind="E4",
                to_email=winner_email,
                context={
                    "full_name": winner_name,
                    "room_name": room_name,
                    "start_at": booking.start_at,
                    "end_at": booking.end_at,
                    "title": booking.title,
                    "booking_id": booking.id,
                },
                related_booking_id=booking.id,
            )
        )

    for victim in attempt.victims:
        events.append(
            EmailEvent(
                kind="E5",
                to_email=victim.owner_email,
                context={
                    # Deliberately no winner identity: spec §12 C11 forbids
                    # telling the victim who displaced them. The only name
                    # here is the recipient's own.
                    "full_name": victim.owner_view["full_name"],
                    "reason": "preempted",
                    "room_name": victim.room_name,
                    "start_at": victim.booking.start_at,
                    "end_at": victim.booking.end_at,
                    "title": victim.booking.title,
                },
                related_booking_id=victim.booking.id,
            )
        )
    return events


def _enqueue(db: Database, events: list[EmailEvent]) -> None:
    """Hand the events to the mailer if it is available."""
    if not events:
        return
    try:
        from app.services import mailer
    except ImportError:  # pragma: no cover - only before Task 2 lands
        return
    mailer.enqueue(db, events)


def log_entries(db: Database, *, user_id: str | None = None, limit: int = 200):
    """Preemption history (spec §6.6 / §6.7).

    ``user_id`` restricts to that member's own records, which is all a
    non-admin may see (spec §3).
    """

    def work(conn: Connection) -> list[dict[str, Any]]:
        clause = ""
        params: list[Any] = []
        if user_id:
            clause = " WHERE p.victim_user_id = ? OR p.winner_user_id = ?"
            params.extend([user_id, user_id])
        params.append(limit)
        return conn.query_all(
            "SELECT p.*, r.name AS room_name,"
            " v.full_name AS victim_name, v.department AS victim_department,"
            " w.full_name AS winner_name, w.department AS winner_department"
            " FROM preemption_log p"
            " JOIN rooms r ON r.id = p.room_id"
            " JOIN users v ON v.id = p.victim_user_id"
            " JOIN users w ON w.id = p.winner_user_id"
            f"{clause} ORDER BY p.occurred_at DESC LIMIT ?",
            tuple(params),
        )

    return db.run_in_transaction(work)
