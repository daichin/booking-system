"""Registration, verification, invitations, approval, and auth (spec FR-1, FR-2).

Implements the state machine from spec §6.1::

    pending_email --verify--> pending_approval --approve--> active
                                               --reject---> rejected
    (invite link)  ------------------------------------> active
    active --admin suspend--> suspended --admin reactivate--> active

Email is never sent from here -- Task 2 owns the transport. Functions whose
contracted return type carries an ``emails`` field (:class:`RegisterResult`,
:class:`InviteResult`) hand the caller :class:`EmailEvent` objects to enqueue
*after* the caller's own commit (CONTRACT.md §3 convention 6). Functions whose
contracted return type is ``None`` or :class:`app.models.User` have nowhere to
put that list, so they enqueue internally through :func:`_dispatch` instead --
see that function's docstring for why, and for the test/dev seam that makes
the resulting tokens observable before Task 2's mailer exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app import i18n, models, security
from app.errors import (
    ACCOUNT_EXISTS,
    ACCOUNT_REJECTED,
    ACCOUNT_SUSPENDED,
    AppError,
    AuthError,
    ConflictError,
    EMAIL_NOT_VERIFIED,
    EMAIL_RATE_LIMITED,
    ForbiddenError,
    INVALID_CREDENTIALS,
    INVALID_EMAIL,
    INVALID_LEVEL,
    INVALID_STATUS_TRANSITION,
    LAST_ADMIN,
    INVITATION_NOT_FOUND,
    LOGIN_RATE_LIMITED,
    MISSING_FIELD,
    NOT_ADMIN,
    NotFoundError,
    RateLimitError,
    TOKEN_EXPIRED,
    TOKEN_INVALID,
    TOKEN_USED,
    USER_NOT_FOUND,
)
from app.config import load_config
from app.services import audit, sessions
from app.settings import Settings
from app.timeutil import now_utc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.base import Connection, Database
    from app.models import User

try:  # Task 2 owns app.services.mailer and may not exist yet.
    from app.services.mailer import EmailEvent
except ImportError:  # pragma: no cover - exercised whenever Task 2 hasn't landed

    @dataclass
    class EmailEvent:  # type: ignore[no-redef]
        """Local stand-in matching CONTRACT.md's ``EmailEvent`` shape."""

        kind: str
        to_email: str
        context: dict
        related_booking_id: str | None = None
        dedupe_key: str | None = None


#: Verification/"already have an account" emails share one rate-limit bucket
#: keyed on the submitted address (spec §6.2): an attacker probing whether an
#: address is registered must see identical behaviour (including when the
#: rate limit itself trips) whether or not an account exists.
_VERIFY_KINDS = ("E1", "E1_EXISTS")
_RESET_KINDS = ("E9",)

#: Spec §6.2: at most 3 verification/reset emails per address per hour.
_EMAIL_RATE_LIMIT = 3
#: Spec §6.2: at most 5 failed logins per email per 15 minutes.
_LOGIN_RATE_LIMIT = 5
_LOGIN_RATE_WINDOW_MINUTES = 15


# --- results carrying emails for the caller to enqueue (CONTRACT.md §5) -----


@dataclass
class RegisterResult:
    emails: list[EmailEvent] = field(default_factory=list)


@dataclass
class InviteResult:
    email: str
    ok: bool
    token_id: str | None = None
    error: str | None = None
    emails: list[EmailEvent] = field(default_factory=list)


# --- internal dispatch seam --------------------------------------------------
#
# resend_verification / approve / reject / request_password_reset are
# contracted (CONTRACT.md §5) to return ``None`` or ``User`` -- there is no
# field to hand an EmailEvent list back through. So they enqueue internally,
# after their own transaction has committed, via _dispatch below.

_dispatched: list[EmailEvent] = []


def _dispatch(db: "Database", events: list[EmailEvent]) -> None:
    """Enqueue ``events`` outside any transaction (never called from inside one).

    Prefers Task 2's real ``app.services.mailer.enqueue`` when it exists.
    Until then, falls back to writing directly into ``email_log`` so the
    email-rate-limit counters (which read that table) still work end to end.

    Every dispatched event is also appended to :data:`_dispatched`, an
    in-process list drained by :func:`drain_dispatched`. This exists purely
    as a test/dev seam: the raw one-time token inside an event's ``context``
    is never persisted anywhere (by design -- only its hash is stored), so
    once a ``None``/``User``-returning function swallows it, nothing else
    could ever recover it to, say, complete a "resend, then use the new
    link" test. Production code should not depend on this once Task 2's
    mailer is the real sink.
    """
    if not events:
        return
    _dispatched.extend(events)
    try:
        from app.services.mailer import enqueue as _enqueue
    except ImportError:
        _fallback_enqueue(db, events)
        return
    _enqueue(db, events)


def _fallback_enqueue(db: "Database", events: list[EmailEvent]) -> None:
    def work(conn: "Connection") -> None:
        now = now_utc()
        for event in events:
            conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " related_booking_id, dedupe_key, attempts, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    models.new_id(),
                    event.to_email,
                    event.kind,
                    event.kind,
                    "queued",
                    event.related_booking_id,
                    event.dedupe_key,
                    0,
                    now,
                ),
            )

    db.run_in_transaction(work)


def drain_dispatched() -> list[EmailEvent]:
    """Pop and return every event handed to :func:`_dispatch` so far.

    Test/dev helper -- see :func:`_dispatch`'s docstring.
    """
    items, _dispatched[:] = list(_dispatched), []
    return items


# --- small internal helpers --------------------------------------------------


def _require(**fields: Any) -> None:
    for name, value in fields.items():
        if value is None or not str(value).strip():
            raise AppError(MISSING_FIELD, {"field": name})


def _require_admin(actor: "User | None") -> None:
    if actor is None or not actor.is_admin:
        raise ForbiddenError(NOT_ADMIN)


def _password_ok(password: str) -> None:
    problem = security.password_problem(password)
    if problem:
        raise AppError(problem)


def _find_user_by_email(conn: "Connection", address: str) -> dict[str, Any] | None:
    return conn.query_one("SELECT * FROM users WHERE email = ?", (address,))


def _reload_user(conn: "Connection", user_id: str) -> "User":
    row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    return models.User.from_row(row)


def _build_link(path: str, raw_token: str) -> str:
    """A full, absolute link embedding a one-time token.

    Task 2's mailer renders emails from a bare ``context`` dict and never
    sees the raw token itself (it is never persisted, see
    :func:`app.security.new_token`) -- so the caller that mints the token
    must hand over a ready-to-click URL, per
    ``app/services/email_templates.py``'s documented context contract
    (``verify_url`` / ``invite_url`` / ``reset_url``).
    """
    return f"{load_config().base_url}{path}?token={raw_token}"


def _check_email_rate_limit(
    conn: "Connection", address: str, kinds: tuple[str, ...]
) -> None:
    """Raise ``EMAIL_RATE_LIMITED`` if ``address`` already hit the cap this hour.

    Counts rows in ``email_log`` rather than any in-process counter, per the
    task brief -- that is also what makes the limit survive process restarts
    and apply uniformly whether or not an account exists for ``address``.
    """
    placeholders = ",".join("?" for _ in kinds)
    since = now_utc() - timedelta(hours=1)
    count = conn.query_value(
        f"SELECT COUNT(*) FROM email_log WHERE to_email = ? AND type IN"
        f" ({placeholders}) AND created_at >= ?",
        (address, *kinds, since),
    )
    if (count or 0) >= _EMAIL_RATE_LIMIT:
        raise RateLimitError(EMAIL_RATE_LIMITED)


# --- registration (spec §6.1 self-registration path) ------------------------


def register(
    db: "Database",
    *,
    email: str,
    password: str,
    full_name: str,
    department: str,
    phone: str,
) -> RegisterResult:
    _require(email=email, full_name=full_name, department=department, phone=phone)
    if not security.is_valid_email(email):
        raise AppError(INVALID_EMAIL)
    _password_ok(password)
    address = security.normalise_email(email)

    def work(conn: "Connection") -> RegisterResult:
        # Checked first, and identically for every branch below, so that a
        # rate-limit response never itself confirms or denies an account's
        # existence (spec §6.1).
        _check_email_rate_limit(conn, address, _VERIFY_KINDS)

        existing = _find_user_by_email(conn, address)
        settings = Settings.load(conn)
        now = now_utc()
        raw, hashed = security.new_token()
        expires_at = now + timedelta(hours=settings.verify_token_hours)

        if existing is None:
            user_id = models.new_id()
            conn.execute(
                "INSERT INTO users (id, email, password_hash, full_name, department,"
                " phone, level, status, is_admin, must_change_password,"
                " email_verified_at, locale, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    address,
                    security.hash_password(password),
                    full_name,
                    department,
                    phone,
                    models.MIN_LEVEL,
                    models.PENDING_EMAIL,
                    False,
                    False,
                    None,
                    i18n.current_locale(),
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO email_tokens (id, user_id, email, type, token_hash,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (models.new_id(), user_id, address, models.VERIFY_EMAIL, hashed,
                 expires_at, now),
            )
            event = EmailEvent(
                kind="E1",
                to_email=address,
                context={
                    "full_name": full_name,
                    "verify_url": _build_link("/verify", raw),
                    "expires_hours": settings.verify_token_hours,
                },
            )
            return RegisterResult(emails=[event])

        if existing["status"] == models.PENDING_EMAIL:
            # Spec §6.1: resend rather than creating a duplicate row.
            conn.execute(
                "INSERT INTO email_tokens (id, user_id, email, type, token_hash,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (models.new_id(), existing["id"], address, models.VERIFY_EMAIL,
                 hashed, expires_at, now),
            )
            event = EmailEvent(
                kind="E1",
                to_email=address,
                context={
                    "full_name": existing["full_name"],
                    "verify_url": _build_link("/verify", raw),
                    "expires_hours": settings.verify_token_hours,
                },
            )
            return RegisterResult(emails=[event])

        # Any other existing status (active, pending_approval, rejected,
        # suspended): the generic "check your email" response, spec §6.1 --
        # never confirm or deny that an account exists.
        event = EmailEvent(
            kind="E1_EXISTS",
            to_email=address,
            context={"full_name": existing["full_name"]},
        )
        return RegisterResult(emails=[event])

    return db.run_in_transaction(work)


def verify_email(db: "Database", raw_token: str) -> "User":
    hashed = security.hash_token(raw_token)

    def work(conn: "Connection") -> "User":
        token = conn.query_one(
            "SELECT * FROM email_tokens WHERE token_hash = ? AND type = ?",
            (hashed, models.VERIFY_EMAIL),
        )
        if token is None:
            raise AppError(TOKEN_INVALID)
        if token["used_at"] is not None:
            raise AppError(TOKEN_USED)
        if token["revoked_at"] is not None:
            raise AppError(TOKEN_INVALID)
        if token["expires_at"] <= now_utc():
            raise AppError(TOKEN_EXPIRED)

        now = now_utc()
        conn.execute(
            "UPDATE email_tokens SET used_at = ? WHERE id = ?", (now, token["id"])
        )
        user_row = conn.query_one(
            "SELECT * FROM users WHERE id = ?", (token["user_id"],)
        )
        if user_row is None:
            raise AppError(TOKEN_INVALID)

        if user_row["status"] == models.PENDING_EMAIL:
            conn.execute(
                "UPDATE users SET status = ?, email_verified_at = ?, updated_at = ?"
                " WHERE id = ?",
                (models.PENDING_APPROVAL, now, now, user_row["id"]),
            )
        elif user_row["email_verified_at"] is None:
            conn.execute(
                "UPDATE users SET email_verified_at = ?, updated_at = ? WHERE id = ?",
                (now, now, user_row["id"]),
            )
        return _reload_user(conn, user_row["id"])

    return db.run_in_transaction(work)


def resend_verification(db: "Database", email: str) -> None:
    address = security.normalise_email(email)

    def work(conn: "Connection") -> list[EmailEvent] | None:
        _check_email_rate_limit(conn, address, _VERIFY_KINDS)
        row = _find_user_by_email(conn, address)
        if row is None or row["status"] != models.PENDING_EMAIL:
            # Privacy: identical (silent) response whether there is no
            # account, or an account that no longer needs verifying.
            return None

        settings = Settings.load(conn)
        now = now_utc()
        raw, hashed = security.new_token()
        conn.execute(
            "INSERT INTO email_tokens (id, user_id, email, type, token_hash,"
            " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                models.new_id(),
                row["id"],
                address,
                models.VERIFY_EMAIL,
                hashed,
                now + timedelta(hours=settings.verify_token_hours),
                now,
            ),
        )
        return [
            EmailEvent(
                kind="E1",
                to_email=address,
                context={
                    "full_name": row["full_name"],
                    "verify_url": _build_link("/verify", raw),
                    "expires_hours": settings.verify_token_hours,
                },
            )
        ]

    events = db.run_in_transaction(work)
    if events:
        _dispatch(db, events)
    return None


# --- invitations (spec §6.1 invitation path) --------------------------------


def invite(
    db: "Database", actor: "User", emails: list[str], level: int | None
) -> list[InviteResult]:
    _require_admin(actor)
    if level is not None and not (models.MIN_LEVEL <= level <= models.MAX_LEVEL):
        raise AppError(INVALID_LEVEL)

    def work(conn: "Connection") -> list[InviteResult]:
        settings = Settings.load(conn)
        now = now_utc()
        expires_at = now + timedelta(hours=settings.invite_token_hours)
        results: list[InviteResult] = []

        for raw_email in emails:
            address = security.normalise_email(raw_email)
            if not security.is_valid_email(address):
                results.append(InviteResult(email=raw_email, ok=False, error=INVALID_EMAIL))
                continue

            if _find_user_by_email(conn, address) is not None:
                # Refuse rather than risk a unique-email conflict later at
                # accept_invitation -- an admin who wants to fast-track an
                # existing applicant should approve them directly instead.
                results.append(InviteResult(email=address, ok=False, error=ACCOUNT_EXISTS))
                continue

            raw, hashed = security.new_token()
            token_id = models.new_id()
            conn.execute(
                "INSERT INTO email_tokens (id, user_id, email, type, token_hash,"
                " invited_level, created_by, expires_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_id,
                    None,
                    address,
                    models.INVITE,
                    hashed,
                    level,
                    actor.id,
                    expires_at,
                    now,
                ),
            )
            audit.record(
                conn,
                actor_id=actor.id,
                action=audit.USER_INVITED,
                target_type="email_token",
                target_id=token_id,
                detail={"email": address, "level": level},
            )
            event = EmailEvent(
                kind="E8",
                to_email=address,
                context={
                    "invite_url": _build_link("/invite", raw),
                    "level": level or models.MIN_LEVEL,
                    "expires_hours": settings.invite_token_hours,
                },
            )
            results.append(
                InviteResult(email=address, ok=True, token_id=token_id, emails=[event])
            )

        return results

    return db.run_in_transaction(work)


def revoke_invitation(db: "Database", actor: "User", token_id: str) -> None:
    _require_admin(actor)

    def work(conn: "Connection") -> None:
        token = conn.query_one(
            "SELECT * FROM email_tokens WHERE id = ? AND type = ?",
            (token_id, models.INVITE),
        )
        if token is None:
            raise NotFoundError(INVITATION_NOT_FOUND)
        if token["used_at"] is not None:
            raise ConflictError(TOKEN_USED)
        if token["revoked_at"] is not None:
            return None  # already revoked: idempotent
        conn.execute(
            "UPDATE email_tokens SET revoked_at = ? WHERE id = ?", (now_utc(), token_id)
        )
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.INVITATION_REVOKED,
            target_type="email_token",
            target_id=token_id,
            detail={"email": token["email"]},
        )
        return None

    return db.run_in_transaction(work)


def accept_invitation(
    db: "Database",
    raw_token: str,
    *,
    password: str,
    full_name: str,
    department: str,
    phone: str,
) -> "User":
    hashed = security.hash_token(raw_token)

    def work(conn: "Connection") -> "User":
        token = conn.query_one(
            "SELECT * FROM email_tokens WHERE token_hash = ? AND type = ?",
            (hashed, models.INVITE),
        )
        if token is None:
            raise AppError(TOKEN_INVALID)
        if token["used_at"] is not None:
            raise AppError(TOKEN_USED)
        if token["revoked_at"] is not None:
            raise AppError(TOKEN_INVALID)
        if token["expires_at"] <= now_utc():
            raise AppError(TOKEN_EXPIRED)

        _require(full_name=full_name, department=department, phone=phone)
        _password_ok(password)

        # The invited email is fixed -- the invitee cannot change it.
        address = token["email"]
        if _find_user_by_email(conn, address) is not None:
            raise AppError(ACCOUNT_EXISTS)

        now = now_utc()
        user_id = models.new_id()
        level = token["invited_level"] or models.MIN_LEVEL
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, department,"
            " phone, level, status, is_admin, must_change_password,"
            " email_verified_at, locale, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                address,
                security.hash_password(password),
                full_name,
                department,
                phone,
                level,
                models.ACTIVE,
                False,
                False,
                now,
                i18n.current_locale(),
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE email_tokens SET used_at = ? WHERE id = ?", (now, token["id"])
        )
        return _reload_user(conn, user_id)

    return db.run_in_transaction(work)


# --- approval state machine (spec §6.1) -------------------------------------


def approve(db: "Database", actor: "User", user_id: str) -> "User":
    _require_admin(actor)

    def work(conn: "Connection") -> "User":
        row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(USER_NOT_FOUND)
        if row["status"] != models.PENDING_APPROVAL:
            raise ConflictError(INVALID_STATUS_TRANSITION)
        now = now_utc()
        conn.execute(
            "UPDATE users SET status = ?, approved_at = ?, approved_by = ?,"
            " updated_at = ? WHERE id = ?",
            (models.ACTIVE, now, actor.id, now, user_id),
        )
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.USER_APPROVED,
            target_type="user",
            target_id=user_id,
        )
        return _reload_user(conn, user_id)

    user = db.run_in_transaction(work)
    _dispatch(
        db,
        [EmailEvent(kind="E2", to_email=user.email, context={"full_name": user.full_name})],
    )
    return user


def reject(db: "Database", actor: "User", user_id: str) -> "User":
    _require_admin(actor)

    def work(conn: "Connection") -> "User":
        row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(USER_NOT_FOUND)
        if row["status"] != models.PENDING_APPROVAL:
            raise ConflictError(INVALID_STATUS_TRANSITION)
        now = now_utc()
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (models.REJECTED, now, user_id),
        )
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.USER_REJECTED,
            target_type="user",
            target_id=user_id,
        )
        return _reload_user(conn, user_id)

    user = db.run_in_transaction(work)
    _dispatch(
        db,
        [EmailEvent(kind="E3", to_email=user.email, context={"full_name": user.full_name})],
    )
    return user


def set_level(db: "Database", actor: "User", user_id: str, level: int) -> "User":
    _require_admin(actor)
    if not (models.MIN_LEVEL <= level <= models.MAX_LEVEL):
        raise AppError(INVALID_LEVEL)

    def work(conn: "Connection") -> "User":
        row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(USER_NOT_FOUND)
        old_level = int(row["level"])
        now = now_utc()
        conn.execute(
            "UPDATE users SET level = ?, updated_at = ? WHERE id = ?",
            (level, now, user_id),
        )
        # Spec FR-3: every level change is audited, and takes effect
        # immediately without touching any existing booking.
        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.LEVEL_CHANGED,
            target_type="user",
            target_id=user_id,
            detail={"from": old_level, "to": level},
        )
        return _reload_user(conn, user_id)

    return db.run_in_transaction(work)


def set_suspended(db: "Database", actor: "User", user_id: str, suspended: bool) -> "User":
    _require_admin(actor)

    def work(conn: "Connection") -> "User":
        row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(USER_NOT_FOUND)
        now = now_utc()

        if suspended:
            if row["status"] == models.SUSPENDED:
                return _reload_user(conn, user_id)  # idempotent
            if row["status"] != models.ACTIVE:
                raise ConflictError(INVALID_STATUS_TRANSITION)
            # Spec §6.1: suspending never touches existing bookings -- an
            # admin cancels them explicitly if that is what they want.
            conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                (models.SUSPENDED, now, user_id),
            )
            audit.record(
                conn,
                actor_id=actor.id,
                action=audit.USER_SUSPENDED,
                target_type="user",
                target_id=user_id,
            )
        else:
            if row["status"] == models.ACTIVE:
                return _reload_user(conn, user_id)  # idempotent
            if row["status"] != models.SUSPENDED:
                raise ConflictError(INVALID_STATUS_TRANSITION)
            conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                (models.ACTIVE, now, user_id),
            )
            audit.record(
                conn,
                actor_id=actor.id,
                action=audit.USER_REACTIVATED,
                target_type="user",
                target_id=user_id,
            )
        return _reload_user(conn, user_id)

    return db.run_in_transaction(work)


# --- authentication (spec §6.2) ---------------------------------------------


def authenticate(db: "Database", email: str, password: str) -> "User":
    """Raises :class:`AuthError` or :class:`RateLimitError` on failure.

    The failed/succeeded ``login_attempts`` row must survive even when the
    outcome is an error, so the rate-limit check works. That means the
    decision to raise happens *after* ``run_in_transaction`` returns --
    raising from inside ``work`` would roll back the very row that records
    the attempt (CONTRACT.md §3 convention 5 warns ``work`` may be retried,
    but an exception it raises still aborts and rolls back that attempt).
    """
    address = security.normalise_email(email)

    def work(conn: "Connection") -> tuple[str, "User | None"]:
        window_start = now_utc() - timedelta(minutes=_LOGIN_RATE_WINDOW_MINUTES)
        failed = conn.query_value(
            "SELECT COUNT(*) FROM login_attempts WHERE email = ? AND succeeded = ?"
            " AND created_at >= ?",
            (address, False, window_start),
        )
        if (failed or 0) >= _LOGIN_RATE_LIMIT:
            return "rate_limited", None

        row = _find_user_by_email(conn, address)
        user = models.User.from_row(row) if row is not None else None
        ok = user is not None and security.verify_password(password, user.password_hash)
        conn.execute(
            "INSERT INTO login_attempts (id, email, succeeded, created_at)"
            " VALUES (?, ?, ?, ?)",
            (models.new_id(), address, ok, now_utc()),
        )
        if not ok:
            return "invalid", None
        return "ok", user

    outcome, user = db.run_in_transaction(work)

    if outcome == "rate_limited":
        raise RateLimitError(LOGIN_RATE_LIMITED)
    if outcome == "invalid":
        raise AuthError(INVALID_CREDENTIALS)

    assert user is not None
    if user.status == models.PENDING_EMAIL:
        raise AuthError(EMAIL_NOT_VERIFIED)
    if user.status == models.SUSPENDED:
        raise AuthError(ACCOUNT_SUSPENDED)
    if user.status == models.REJECTED:
        raise AuthError(ACCOUNT_REJECTED)
    # pending_approval and active may both log in (spec §3): a pending member
    # reaches the read-only day/week views. must_change_password does not
    # block login either (spec §10.3) -- the caller inspects the returned
    # User and redirects to /password itself.
    return user


# --- passwords (spec §6.2) --------------------------------------------------


def request_password_reset(db: "Database", email: str) -> None:
    address = security.normalise_email(email)

    def work(conn: "Connection") -> list[EmailEvent] | None:
        _check_email_rate_limit(conn, address, _RESET_KINDS)
        row = _find_user_by_email(conn, address)
        if row is None:
            return None  # privacy: identical silent response

        settings = Settings.load(conn)
        now = now_utc()
        raw, hashed = security.new_token()
        conn.execute(
            "INSERT INTO email_tokens (id, user_id, email, type, token_hash,"
            " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                models.new_id(),
                row["id"],
                address,
                models.PASSWORD_RESET,
                hashed,
                now + timedelta(hours=settings.reset_token_hours),
                now,
            ),
        )
        return [
            EmailEvent(
                kind="E9",
                to_email=address,
                context={
                    "full_name": row["full_name"],
                    "reset_url": _build_link("/reset", raw),
                    "expires_hours": settings.reset_token_hours,
                },
            )
        ]

    events = db.run_in_transaction(work)
    if events:
        _dispatch(db, events)
    return None


def reset_password(db: "Database", raw_token: str, new_password: str) -> "User":
    _password_ok(new_password)
    hashed = security.hash_token(raw_token)

    def work(conn: "Connection") -> "User":
        token = conn.query_one(
            "SELECT * FROM email_tokens WHERE token_hash = ? AND type = ?",
            (hashed, models.PASSWORD_RESET),
        )
        if token is None:
            raise AppError(TOKEN_INVALID)
        if token["used_at"] is not None:
            raise AppError(TOKEN_USED)
        if token["revoked_at"] is not None:
            raise AppError(TOKEN_INVALID)
        if token["expires_at"] <= now_utc():
            raise AppError(TOKEN_EXPIRED)

        now = now_utc()
        conn.execute(
            "UPDATE email_tokens SET used_at = ? WHERE id = ?", (now, token["id"])
        )
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ?,"
            " updated_at = ? WHERE id = ?",
            (security.hash_password(new_password), False, now, token["user_id"]),
        )
        audit.record(
            conn,
            actor_id=token["user_id"],
            action=audit.PASSWORD_RESET,
            target_type="user",
            target_id=token["user_id"],
        )
        return _reload_user(conn, token["user_id"])

    user = db.run_in_transaction(work)
    # Spec §12 A7: a successful reset invalidates every existing session.
    sessions.revoke_all_for_user(db, user.id)
    return user


def change_password(
    db: "Database", user: "User", current_password: str, new_password: str
) -> "User":
    if not security.verify_password(current_password, user.password_hash):
        raise AuthError(INVALID_CREDENTIALS)
    _password_ok(new_password)

    def work(conn: "Connection") -> "User":
        now = now_utc()
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ?,"
            " updated_at = ? WHERE id = ?",
            (security.hash_password(new_password), False, now, user.id),
        )
        return _reload_user(conn, user.id)

    return db.run_in_transaction(work)


# --- account deletion --------------------------------------------------------
#
# Deletion is anonymisation, not removal. Every booking, preemption record and
# audit entry references users.id, and the cross-cutting rule is that those are
# kept forever -- a DELETE would either cascade the history away or fail on the
# foreign keys. So the row survives as a tombstone with every personal detail
# scrubbed, and the history that points at it still resolves, now to a member
# with no name and no address.


#: What a tombstone's name and contact fields become. Stored rather than
#: translated at render time: the value ends up in exported CSV, in the audit
#: trail and in a booking history that other people read, and a single stored
#: string is the same everywhere. It follows the default language, like the
#: rest of the seeded content.
def _tombstone_name() -> str:
    return i18n.t("account.deleted_member", locale=i18n.DEFAULT_LOCALE)


def _released_address(user_id: str) -> str:
    """A placeholder that frees the real address for re-registration.

    ``.invalid`` is reserved by RFC 2606 and can never be delivered to, and
    the id keeps it unique against ``ux_users_email``.
    """
    return f"deleted+{user_id}@deleted.invalid"


@dataclass
class DeleteResult:
    user_id: str
    #: Future confirmed bookings released so the rooms are usable again.
    cancelled_bookings: int


def delete_account(
    db: "Database",
    *,
    actor: "User",
    user_id: str,
    current_password: str | None = None,
) -> DeleteResult:
    """Anonymise an account, keeping every historical record that names it.

    A member deletes their own account by proving they still hold the
    password; an admin may delete anyone's without it. The member's future
    confirmed bookings are cancelled in the same transaction, because leaving
    them would hold rooms that nobody can now release -- the owner can no
    longer log in to cancel them.

    No email is sent. The mailbox has just been disowned, and telling a
    deleted address about the bookings it lost is both useless and a way of
    keeping the address in the outbound log.
    """
    deleting_self = actor.id == user_id
    if not deleting_self and not actor.is_admin:
        raise ForbiddenError(NOT_ADMIN)
    if deleting_self:
        if not current_password or not security.verify_password(
            current_password, actor.password_hash
        ):
            raise AuthError(INVALID_CREDENTIALS)

    def work(conn: "Connection") -> DeleteResult:
        row = conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(USER_NOT_FOUND)
        if row["deleted_at"] is not None:
            # The row is already a tombstone. Reporting "not found" is the
            # truth from the caller's point of view and keeps the operation
            # from being repeatable against an account that no longer exists.
            raise NotFoundError(USER_NOT_FOUND)

        # An installation with no administrator cannot approve members, edit
        # settings or recover itself, and nothing in the UI could undo it.
        if row["is_admin"]:
            remaining = conn.query_value(
                "SELECT COUNT(*) FROM users WHERE is_admin = ? AND status = ?"
                " AND deleted_at IS NULL AND id <> ?",
                (True, models.ACTIVE, user_id),
            )
            if not remaining:
                raise ConflictError(LAST_ADMIN)

        now = now_utc()

        # Release the rooms first, while the owner is still identifiable.
        upcoming = conn.query_all(
            "SELECT id FROM bookings WHERE user_id = ? AND status = ? AND end_at > ?",
            (user_id, models.CONFIRMED, now),
        )
        cancelled_status = (
            models.CANCELLED_BY_USER if deleting_self else models.CANCELLED_BY_ADMIN
        )
        for booking in upcoming:
            conn.execute(
                "UPDATE bookings SET status = ?, cancelled_at = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (cancelled_status, now, now, booking["id"], models.CONFIRMED),
            )
            # The reminder would arrive for a meeting that is no longer
            # booked, addressed to a mailbox we have just disowned.
            conn.execute(
                "UPDATE email_log SET status = 'skipped',"
                " error = 'account deleted' WHERE dedupe_key = ? AND status = 'queued'",
                (f"reminder:{booking['id']}",),
            )

        conn.execute(
            "UPDATE users SET email = ?, password_hash = ?, full_name = ?,"
            " department = ?, phone = ?, status = ?, is_admin = ?,"
            " must_change_password = ?, deleted_at = ?, updated_at = ?"
            " WHERE id = ?",
            (
                _released_address(user_id),
                # Not a blank or a constant: a value nobody has ever seen and
                # nobody can produce, so no password matches this row again.
                security.hash_password(security.new_token()[0]),
                _tombstone_name(),
                "-",
                "-",
                models.SUSPENDED,
                False,
                False,
                now,
                now,
                user_id,
            ),
        )

        # Anything still holding the door open.
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, user_id),
        )
        conn.execute(
            "UPDATE email_tokens SET revoked_at = ?"
            " WHERE user_id = ? AND used_at IS NULL AND revoked_at IS NULL",
            (now, user_id),
        )

        # The delivery log is history and stays, but the address in it is the
        # single most identifying thing we hold; keeping it would leave the
        # account deleted in name only. Queued mail to it is dropped.
        conn.execute(
            "UPDATE email_log SET status = 'skipped', error = 'account deleted'"
            " WHERE to_email = ? AND status = 'queued'",
            (row["email"],),
        )
        conn.execute(
            "UPDATE email_log SET to_email = ? WHERE to_email = ?",
            (_released_address(user_id), row["email"]),
        )

        audit.record(
            conn,
            actor_id=actor.id,
            action=audit.USER_DELETED,
            target_type="user",
            target_id=user_id,
            detail={
                "self_service": deleting_self,
                "bookings_cancelled": len(upcoming),
            },
        )
        return DeleteResult(user_id=user_id, cancelled_bookings=len(upcoming))

    return db.run_in_transaction(work)


__all__ = [
    "EmailEvent",
    "RegisterResult",
    "InviteResult",
    "register",
    "verify_email",
    "resend_verification",
    "invite",
    "revoke_invitation",
    "accept_invitation",
    "approve",
    "reject",
    "set_level",
    "set_suspended",
    "authenticate",
    "request_password_reset",
    "reset_password",
    "change_password",
    "delete_account",
    "DeleteResult",
    "drain_dispatched",
]
