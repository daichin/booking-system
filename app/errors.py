"""Error and outcome codes.

Every code here is part of the API contract (see CONTRACT.md). Handlers return
codes; the zh-TW wording lives in :mod:`app.i18n` so that strings can be
swapped without touching business logic.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """A business-rule failure with a stable machine-readable code.

    ``details`` carries structured context for the UI (for example the
    blocking booking in a preemption refusal). It must never contain data the
    requester is not allowed to see -- spec §7.2 forbids revealing a blocking
    member's email address.
    """

    #: HTTP status used when this error escapes to the web layer.
    status = 400

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code}
        if self.details:
            payload["details"] = self.details
        return payload


class AuthError(AppError):
    status = 401


class ForbiddenError(AppError):
    status = 403


class NotFoundError(AppError):
    status = 404


class ConflictError(AppError):
    status = 409


class RateLimitError(AppError):
    status = 429


# --- Booking validation, in the order spec §6.5 mandates -------------------

NOT_ACTIVE = "NOT_ACTIVE"                    # step 1: requester not approved
ROOM_INACTIVE = "ROOM_INACTIVE"              # step 2
ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
OFF_GRID = "OFF_GRID"                        # step 3: not on a slot boundary
END_NOT_AFTER_START = "END_NOT_AFTER_START"  # step 3
TOO_LONG = "TOO_LONG"                        # step 4: exceeds max_booking_minutes
START_IN_PAST = "START_IN_PAST"              # step 5
BEYOND_HORIZON = "BEYOND_HORIZON"            # step 6
OUTSIDE_WINDOW = "OUTSIDE_WINDOW"            # step 7: outside room open/close
CROSSES_MIDNIGHT = "CROSSES_MIDNIGHT"        # step 7
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"            # step 8
TITLE_REQUIRED = "TITLE_REQUIRED"

# --- Conflict resolution, spec §7 ------------------------------------------

SELF_OVERLAP = "SELF_OVERLAP"
EQUAL_OR_HIGHER_LEVEL = "EQUAL_OR_HIGHER_LEVEL"
PROTECTED_WINDOW = "PROTECTED_WINDOW"

#: Phase-1 outcomes returned by the preemption engine (spec §7.2).
AVAILABLE = "AVAILABLE"
PREEMPTION_REQUIRED = "PREEMPTION_REQUIRED"
BLOCKED = "BLOCKED"
CREATED = "CREATED"

# --- Bookings ---------------------------------------------------------------

BOOKING_NOT_FOUND = "BOOKING_NOT_FOUND"
BOOKING_NOT_CONFIRMED = "BOOKING_NOT_CONFIRMED"
BOOKING_ALREADY_ENDED = "BOOKING_ALREADY_ENDED"
NOT_BOOKING_OWNER = "NOT_BOOKING_OWNER"

# --- Accounts and auth, spec §6.1 / §6.2 ------------------------------------

INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
ACCOUNT_REJECTED = "ACCOUNT_REJECTED"
EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
PASSWORD_TOO_SHORT = "PASSWORD_TOO_SHORT"
PASSWORD_TOO_COMMON = "PASSWORD_TOO_COMMON"
PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
MISSING_FIELD = "MISSING_FIELD"
INVALID_EMAIL = "INVALID_EMAIL"
TOKEN_INVALID = "TOKEN_INVALID"
TOKEN_EXPIRED = "TOKEN_EXPIRED"
TOKEN_USED = "TOKEN_USED"
LOGIN_RATE_LIMITED = "LOGIN_RATE_LIMITED"
EMAIL_RATE_LIMITED = "EMAIL_RATE_LIMITED"
NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
NOT_ADMIN = "NOT_ADMIN"

# --- Admin / rooms / settings ----------------------------------------------

USER_NOT_FOUND = "USER_NOT_FOUND"
INVALID_LEVEL = "INVALID_LEVEL"
INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
#: Refuses to delete the only administrator: an installation without one
#: cannot approve members, edit settings, or undo the deletion.
LAST_ADMIN = "LAST_ADMIN"
ROOM_HAS_BOOKINGS = "ROOM_HAS_BOOKINGS"
INVALID_SETTING = "INVALID_SETTING"
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"

# --- Accounts, added by Task 1 -----------------------------------------------
# ACCOUNT_EXISTS: an admin-issued invitation or invite-acceptance targets an
#   email that already has a user row. (Self-registration never raises this --
#   spec §6.1 requires a generic response there instead, see accounts.register.)
# INVITATION_NOT_FOUND: revoke_invitation() referenced an unknown token id.
ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
INVITATION_NOT_FOUND = "INVITATION_NOT_FOUND"
