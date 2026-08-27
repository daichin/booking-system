"""Domain types (spec §4).

Thin dataclasses over database rows. They carry no persistence logic; services
own that. Status values are module constants rather than ``enum.Enum`` so they
compare directly against what the database stores.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# --- user status, spec §6.1 -------------------------------------------------

PENDING_EMAIL = "pending_email"
PENDING_APPROVAL = "pending_approval"
ACTIVE = "active"
REJECTED = "rejected"
SUSPENDED = "suspended"

USER_STATUSES = (PENDING_EMAIL, PENDING_APPROVAL, ACTIVE, REJECTED, SUSPENDED)

# --- booking status, spec §4.4 ---------------------------------------------

CONFIRMED = "confirmed"
CANCELLED_BY_USER = "cancelled_by_user"
CANCELLED_BY_ADMIN = "cancelled_by_admin"
PREEMPTED = "preempted"

BOOKING_STATUSES = (CONFIRMED, CANCELLED_BY_USER, CANCELLED_BY_ADMIN, PREEMPTED)

# --- token types, spec §4.2 -------------------------------------------------

VERIFY_EMAIL = "verify_email"
INVITE = "invite"
PASSWORD_RESET = "password_reset"

MIN_LEVEL = 1
MAX_LEVEL = 10


def new_id() -> str:
    """A fresh canonical UUID string, the primary-key form used everywhere."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    full_name: str
    department: str
    phone: str
    level: int
    status: str
    is_admin: bool
    must_change_password: bool
    #: Interface language. Stored per member because the reminder job
    #: sends mail with no browser to ask.
    locale: str
    email_verified_at: datetime | None
    approved_at: datetime | None
    approved_by: str | None
    created_at: datetime
    updated_at: datetime
    #: Set when the account was deleted. The row survives because booking
    #: history, preemption records and the audit trail all reference it; its
    #: personal details are scrubbed instead. A tombstone can never be
    #: active, reactivated, or logged into.
    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE and not self.is_deleted

    @property
    def can_book(self) -> bool:
        """Spec §3: only approved members may create bookings."""
        return self.status == ACTIVE and not self.is_deleted

    def public_view(self) -> dict[str, Any]:
        """Fields safe to show another member.

        Spec §7.2: a blocked requester may see the blocking member's name and
        department, never their email address.
        """
        return {
            "id": self.id,
            "full_name": self.full_name,
            "department": self.department,
            "level": self.level,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "User":
        return cls(
            id=row["id"],
            email=row["email"],
            password_hash=row["password_hash"],
            full_name=row["full_name"],
            department=row["department"],
            phone=row["phone"],
            level=int(row["level"]),
            status=row["status"],
            is_admin=bool(row["is_admin"]),
            must_change_password=bool(row["must_change_password"]),
            locale=row.get("locale") or "zh-TW",
            email_verified_at=row["email_verified_at"],
            approved_at=row["approved_at"],
            approved_by=row["approved_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row.get("deleted_at"),
        )


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    capacity: int | None
    location: str | None
    equipment_note: str | None
    is_active: bool
    open_minutes: int | None
    close_minutes: int | None
    created_at: datetime
    updated_at: datetime

    def window(self, settings: Any) -> tuple[int, int]:
        """Bookable window in minutes past local midnight.

        Falls back to the global default in spec §5 when the room does not
        override it.
        """
        open_at = (
            self.open_minutes
            if self.open_minutes is not None
            else settings.default_open_minutes
        )
        close_at = (
            self.close_minutes
            if self.close_minutes is not None
            else settings.default_close_minutes
        )
        return open_at, close_at

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Room":
        return cls(
            id=row["id"],
            name=row["name"],
            capacity=row["capacity"],
            location=row["location"],
            equipment_note=row["equipment_note"],
            is_active=bool(row["is_active"]),
            open_minutes=row["open_minutes"],
            close_minutes=row["close_minutes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class Booking:
    id: str
    room_id: str
    user_id: str
    title: str
    start_at: datetime
    end_at: datetime
    status: str
    level_at_booking: int
    preempted_by_booking_id: str | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_confirmed(self) -> bool:
        return self.status == CONFIRMED

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Booking":
        return cls(
            id=row["id"],
            room_id=row["room_id"],
            user_id=row["user_id"],
            title=row["title"],
            start_at=row["start_at"],
            end_at=row["end_at"],
            status=row["status"],
            level_at_booking=int(row["level_at_booking"]),
            preempted_by_booking_id=row["preempted_by_booking_id"],
            cancelled_at=row["cancelled_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
