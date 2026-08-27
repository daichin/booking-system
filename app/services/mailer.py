"""Email service (spec §9). See CONTRACT.md §5 "Task 2" for the binding
interface: :class:`EmailEvent`, :func:`enqueue`, :func:`send_pending`,
:func:`run_reminders`, :func:`run_admin_digest`.

Design notes
------------
``email_log`` (spec §4.7) has no column for a rendered message *body* -- only
``subject``, for admin visibility. What it does carry is ``context``: the
values a template needs to render itself (recipient name, booking time, the
URL a one-time token is embedded in). That column exists because the previous
design kept the context in an in-process dict instead, and that was wrong.

The reasoning had been that the web process is long-lived, so a queued row
would be flushed by the same process that created it. On Render's free tier
it is not: the process sleeps after 15 minutes idle, and the queue is flushed
by a cron call on the same 15-minute cadence, so the sending process was
usually a *fresh* one with an empty cache. Every such message was marked
``failed`` with ``context_unavailable`` -- without consuming a single retry,
and with no way to reconstruct it. Mail lost that way included E5 "your
booking was preempted", which the spec requires be delivered.

Persisting the context means any process can render any queued message, so a
retry survives a restart and an admin can force a resend. Two consequences:

1. One-time secrets (verification/invite/reset tokens) are only ever known to
   the caller that minted them, so they must already be embedded as full URLs
   in ``EmailEvent.context`` by the time it reaches :func:`enqueue` -- and
   they therefore land in the database. :data:`_TOKEN_KINDS` names the kinds
   that carry one, and their context is dropped the moment delivery succeeds,
   so no live token outlives the mail that carried it.
2. Everything else keeps its context, which is what makes the admin resend
   button work for the messages people actually ask to have sent again.

Retries never sleep in-process (spec instruction): a failed send increments
``email_log.attempts`` and leaves the row ``queued`` until the attempt budget
is spent, at which point it becomes ``failed``. A later call to
:func:`send_pending` -- from the request that queued it, from the next
request, or from the reminder cron -- is what actually retries it.

Emails are only ever sent from outside a database transaction (CONTRACT.md
§3 rule 6): every write to ``email_log`` is its own short transaction, wrapped
tightly around the transport call rather than the caller's business
transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from contextlib import contextmanager

from app import i18n, models
from app.db.base import Connection, Database
from app.errors import (
    ConflictError,
    EMAIL_LOG_NOT_FOUND,
    EMAIL_NOT_RESENDABLE,
    NotFoundError,
)
from app.services import email_templates
from app.services.transports import Message, Transport, build_transport
from app.settings import Settings
from app.timeutil import local_date, now_utc, taipei_midnight

#: Spec §9.4 guarantees these still send once the cap is reached, because
#: losing them would lock someone out of their account or leave them unaware
#: that a booking of theirs was cancelled.
_CRITICAL_KINDS = frozenset({"E1", "E5", "E8", "E9"})

#: Reminders are "dropped first" (§9.4). That ordering is enforced by
#: delivering in priority order -- critical, then ordinary, then reminders --
#: so a day that only slightly exceeds the cap loses reminders and nothing
#: else. Anything non-critical still queued once the cap is reached is
#: dropped too, otherwise the cap would not actually bound the daily volume
#: and the provider's own 300/day limit (constraint C5) would start rejecting
#: mail instead.
_PRIORITY_ORDER = (
    "CASE WHEN type IN ('E1','E5','E8','E9') THEN 0"
    " WHEN type = 'E10' THEN 2 ELSE 1 END"
)

#: Kinds whose context contains a live one-time token embedded in a URL.
#: Their context is discarded as soon as delivery succeeds, so a token never
#: outlives the message carrying it. They are also the kinds an admin never
#: needs to resend: the member can ask for a fresh link themselves, and a new
#: token is a better answer than a copy of an old one.
_TOKEN_KINDS = frozenset({"E1", "E8", "E9"})

#: Attempts before a failed send becomes terminal (spec §9.4: "retried up to
#: 3 times ... then marked failed"). The spec's 3 is the default; it is a
#: setting because how many attempts are worth making depends on how reliable
#: the provider turns out to be, which is not knowable from here.
MAX_ATTEMPTS = 3

#: At most one E7 digest per admin per hour (spec §9.4 / §12 D4).
DIGEST_INTERVAL = timedelta(hours=1)


@dataclass
class EmailEvent:
    """One email to be sent, as described in CONTRACT.md §5 "Task 2"."""

    kind: str
    to_email: str
    context: dict[str, Any] = field(default_factory=dict)
    related_booking_id: str | None = None
    dedupe_key: str | None = None


@dataclass
class SendReport:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass
class ReminderReport:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


# --- render context, stored on the row --------------------------------------

#: Marks an encoded datetime. A bare ISO string would be indistinguishable
#: from a caller's own string and would come back as the wrong type, which
#: the templates would then format as a raw timestamp.
_DT_KEY = "__datetime__"


def _encode_context(context: dict[str, Any]) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, datetime):
            return {_DT_KEY: value.isoformat()}
        raise TypeError(
            f"email context value of type {type(value).__name__} cannot be"
            " stored; add an encoding for it rather than dropping it"
        )

    return json.dumps(context, default=default)


def _decode_context(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None

    def hook(obj: dict[str, Any]) -> Any:
        if len(obj) == 1 and _DT_KEY in obj:
            return datetime.fromisoformat(obj[_DT_KEY])
        return obj

    return json.loads(raw, object_hook=hook)


#: Set by :func:`enqueue` so the web layer knows a flush is worth doing after
#: the response is built, instead of querying for queued mail on every single
#: request. Only ever a hint: a stale True costs one cheap query, and the
#: reminder cron flushes regardless.
_pending_hint = threading.Event()


def note_pending() -> None:
    _pending_hint.set()


def take_pending_hint() -> bool:
    """True (once) if mail may be waiting. Clears the hint."""
    was_set = _pending_hint.is_set()
    _pending_hint.clear()
    return was_set


# --- transport selection -----------------------------------------------------

_transport_lock = threading.Lock()
_default_transport_instance: Transport | None = None


def _default_transport() -> Transport:
    """The process-wide transport, built from :mod:`app.config` on first use.

    Tests always pass an explicit ``transport=FakeTransport()`` and never
    rely on this, per the environment constraint that ``BrevoTransport`` must
    never be exercised locally.
    """
    global _default_transport_instance
    with _transport_lock:
        if _default_transport_instance is None:
            from app.config import load_config  # local import avoids a cycle

            _default_transport_instance = build_transport(load_config())
        return _default_transport_instance


def set_default_transport(transport: Transport | None) -> None:
    """Override (or clear, with ``None``) the process-wide transport.

    Exposed mainly so a long-lived process can swap transports without
    restarting; tests should prefer passing ``transport=`` explicitly.
    """
    global _default_transport_instance
    with _transport_lock:
        _default_transport_instance = transport


# --- helpers -----------------------------------------------------------------


def _is_unique_violation(exc: BaseException) -> bool:
    """True for a unique-constraint violation on either backend.

    SQLite raises :class:`sqlite3.IntegrityError` directly. Postgres (via
    psycopg) raises ``psycopg.errors.UniqueViolation``; it is matched by
    class name so this module does not need an unconditional psycopg import.
    """
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return type(exc).__name__ == "UniqueViolation"


def _count_sent_today(conn: Connection) -> int:
    """Emails already sent today (Taipei calendar day), for the cap guard."""
    start_of_day = taipei_midnight(local_date(now_utc()))
    value = conn.query_value(
        "SELECT COUNT(*) FROM email_log WHERE status = 'sent' AND created_at >= ?",
        (start_of_day,),
    )
    return int(value or 0)


def _update_row(db: Database, row_id: str, **fields: Any) -> None:
    assignments = ", ".join(f"{key} = ?" for key in fields)
    params = tuple(fields.values()) + (row_id,)

    def work(conn: Connection) -> None:
        conn.execute(f"UPDATE email_log SET {assignments} WHERE id = ?", params)

    db.run_in_transaction(work)


def _record_cron_run(db: Database, started_at: Any, *, ok: bool, detail: str) -> None:
    def work(conn: Connection) -> None:
        conn.execute(
            "INSERT INTO cron_runs (id, job, started_at, finished_at, ok, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (models.new_id(), "send_reminders", started_at, now_utc(), ok, detail),
        )

    db.run_in_transaction(work)


def _locale_for(db: Database, to_email: str) -> str:
    """The recipient's chosen interface language.

    Mail is rendered in the language of whoever receives it, not of whoever
    triggered it -- and the reminder job runs from cron with no request to
    inherit a locale from, which is exactly why the preference is stored on
    the member rather than in a cookie.
    """

    def work(conn: Connection) -> str | None:
        row = conn.query_one("SELECT locale FROM users WHERE email = ?", (to_email,))
        return row["locale"] if row else None

    try:
        return i18n.normalise(db.run_in_transaction(work))
    except Exception:  # noqa: BLE001 - a missing row must not stop the mail
        return i18n.DEFAULT_LOCALE


@contextmanager
def _rendering_for(locale: str):
    """Render this message in ``locale``, then put the locale back."""
    previous = i18n.current_locale()
    i18n.set_locale(locale)
    try:
        yield
    finally:
        i18n.set_locale(previous)


def _attempt_send(
    db: Database,
    row: dict[str, Any],
    context: dict[str, Any],
    transport: Transport,
    max_attempts: int,
) -> bool | None:
    """Try to deliver one queued row. Returns ``True`` (sent), ``False``
    (exhausted retries, now ``failed``), or ``None`` (transient failure,
    still ``queued`` for a later call)."""
    with _rendering_for(_locale_for(db, row["to_email"])):
        rendered = email_templates.render(row["type"], context)
    message = Message(
        to_email=row["to_email"],
        to_name=context.get("full_name"),
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
    )
    result = transport.send(message)
    attempts = int(row["attempts"]) + 1

    if result.ok:
        delivered: dict[str, Any] = {
            "status": "sent",
            "provider_message_id": result.message_id,
            "error": None,
            "attempts": attempts,
            "sent_at": now_utc(),
        }
        if row["type"] in _TOKEN_KINDS:
            # Delivered, so the link has reached the person it was minted
            # for. A live token has no business staying in the log.
            delivered["context"] = None
        _update_row(db, row["id"], **delivered)
        return True

    # A failure keeps its context, whether or not the budget is spent. That
    # is what lets the next call -- the next request, or the next cron tick
    # -- pick the row up and try again with nobody intervening, and what
    # gives the admin's resend button something to work with.
    terminal = attempts >= max_attempts
    _update_row(
        db,
        row["id"],
        status="failed" if terminal else "queued",
        error=result.error,
        attempts=attempts,
    )
    return False if terminal else None


def _mark_preemption_notified(db: Database, row: dict[str, Any]) -> None:
    """Stamp ``preemption_log.notification_sent_at`` once E5 actually goes out.

    Spec §4.5 carries this column, and it is only meaningful at the moment of
    a successful send -- enqueuing is not notifying.
    """
    if row["type"] != "E5" or not row["related_booking_id"]:
        return

    def work(conn: Connection) -> None:
        conn.execute(
            "UPDATE preemption_log SET notification_sent_at = ?"
            " WHERE victim_booking_id = ? AND notification_sent_at IS NULL",
            (now_utc(), row["related_booking_id"]),
        )

    db.run_in_transaction(work)


def _send_row(
    db: Database,
    row: dict[str, Any],
    transport: Transport,
    sent_today: list[int],
    cap: int,
    max_attempts: int,
) -> str:
    """Deliver (or skip/fail) one queued row. Returns "sent"/"failed"/"skipped"."""
    context = _decode_context(row.get("context"))
    if context is None:
        # Only reachable for a row queued before this column existed, or one
        # already delivered. Its content is genuinely unrecoverable, so say
        # so rather than retrying something that can never succeed.
        _update_row(db, row["id"], status="failed", error="context_unavailable")
        return "failed"

    if row["type"] not in _CRITICAL_KINDS and sent_today[0] >= cap:
        _update_row(db, row["id"], status="skipped", error="daily_cap_reached")
        return "skipped"

    outcome = _attempt_send(db, row, context, transport, max_attempts)
    if outcome is True:
        sent_today[0] += 1
        _mark_preemption_notified(db, row)
        return "sent"
    if outcome is False:
        return "failed"
    return "failed"  # transient failure this call; row stays queued for retry


# --- public interface (CONTRACT.md §5 "Task 2") ------------------------------


def enqueue(db: Database, events: list[EmailEvent]) -> list[str]:
    """Persist ``events`` as ``queued`` rows in ``email_log``.

    Never sends. Rendering only happens far enough to compute the subject
    line stored for admin visibility; the full render happens at send time.

    An event with a ``dedupe_key`` that collides with an existing row (the
    unique index in the schema) is silently dropped rather than raising --
    this is what makes :func:`run_reminders` idempotent under concurrent
    invocation (spec §12 D1).
    """
    if not events:
        return []

    def work(conn: Connection) -> list[str | None]:
        results: list[str | None] = []
        for event in events:
            row_id = models.new_id()
            with _rendering_for(_locale_for(db, event.to_email)):
                subject = email_templates.render(event.kind, event.context).subject
            # Each insert gets its own savepoint. On Postgres a failed
            # statement poisons the whole transaction, so simply catching the
            # unique violation and carrying on would make the *next* insert
            # raise InFailedSqlTransaction. Rolling back to the savepoint
            # leaves the transaction usable on both backends.
            conn.execute("SAVEPOINT email_enqueue")
            try:
                conn.execute(
                    "INSERT INTO email_log (id, to_email, type, subject, status,"
                    " related_booking_id, dedupe_key, attempts, created_at,"
                    " context)"
                    " VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?)",
                    (
                        row_id,
                        event.to_email,
                        event.kind,
                        subject,
                        event.related_booking_id,
                        event.dedupe_key,
                        now_utc(),
                        _encode_context(event.context),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - narrowed immediately below
                conn.execute("ROLLBACK TO SAVEPOINT email_enqueue")
                conn.execute("RELEASE SAVEPOINT email_enqueue")
                if event.dedupe_key and _is_unique_violation(exc):
                    results.append(None)
                    continue
                raise
            conn.execute("RELEASE SAVEPOINT email_enqueue")
            results.append(row_id)
        return results

    results = db.run_in_transaction(work)

    ids = [row_id for row_id in results if row_id is not None]
    if ids:
        note_pending()
    return ids


def send_pending(
    db: Database, *, limit: int = 50, transport: Transport | None = None
) -> SendReport:
    """Attempt delivery of queued emails (spec §9.4).

    Honours the daily cap (E10 dropped first, logged ``skipped``) and the
    3-attempt retry budget (a transient failure leaves the row ``queued`` for
    a future call rather than sleeping in-process).
    """
    transport = transport or _default_transport()
    settings = db.run_in_transaction(Settings.load)
    cap = settings.daily_email_cap
    max_attempts = settings.email_max_attempts
    sent_today = [db.run_in_transaction(_count_sent_today)]

    rows = db.run_in_transaction(
        lambda conn: conn.query_all(
            "SELECT * FROM email_log WHERE status = 'queued'"
            f" ORDER BY {_PRIORITY_ORDER}, created_at LIMIT ?",
            (limit,),
        )
    )

    report = SendReport()
    for row in rows:
        outcome = _send_row(db, row, transport, sent_today, cap, max_attempts)
        if outcome == "sent":
            report.sent += 1
        elif outcome == "skipped":
            report.skipped += 1
        else:
            report.failed += 1
    return report


def can_resend(row: dict[str, Any]) -> bool:
    """Whether this log row still holds enough to be sent again.

    False once a one-time link has been delivered and its context dropped.
    That is not a limitation to work around: reissuing a fresh verification
    or reset link is both safer and already available to the member.
    """
    return bool(row.get("context"))


def resend(db: Database, row_id: str, *, transport: Transport | None = None) -> bool:
    """Queue a logged email again and try to deliver it now.

    For an admin acting on "I deleted that email, can you send it again?".
    Retrying after a failure needs no button -- a failed row keeps its
    context and the next flush picks it up on its own.

    Raises :class:`NotFoundError` if the row is gone and
    :class:`ConflictError` if it can no longer be rendered.
    """

    def load(conn: Connection) -> dict[str, Any] | None:
        return conn.query_one("SELECT * FROM email_log WHERE id = ?", (row_id,))

    row = db.run_in_transaction(load)
    if row is None:
        raise NotFoundError(EMAIL_LOG_NOT_FOUND)
    if not can_resend(row):
        raise ConflictError(EMAIL_NOT_RESENDABLE)

    # Attempts start over: this is a fresh decision by a person, not a
    # continuation of the automatic retry budget that already ran out.
    _update_row(db, row_id, status="queued", error=None, attempts=0, sent_at=None)
    note_pending()
    report = send_pending(db, limit=1, transport=transport)
    return report.sent == 1


def run_reminders(db: Database, *, transport: Transport | None = None) -> ReminderReport:
    """E10 reminders (spec §9.3). Idempotent; records a ``cron_runs`` row."""
    started = now_utc()
    transport = transport or _default_transport()
    report = ReminderReport()
    settings = db.run_in_transaction(Settings.load)

    if not settings.reminders_enabled:
        _record_cron_run(db, started, ok=True, detail="reminders_disabled")
        return report

    window_end = started + timedelta(minutes=settings.reminder_lead_minutes)
    cap = settings.daily_email_cap
    max_attempts = settings.email_max_attempts
    sent_today = [db.run_in_transaction(_count_sent_today)]

    try:
        due = db.run_in_transaction(
            lambda conn: conn.query_all(
                "SELECT b.id AS booking_id, b.title, b.start_at, b.end_at,"
                " u.email AS owner_email, u.full_name AS owner_name,"
                " r.name AS room_name"
                " FROM bookings b"
                " JOIN users u ON u.id = b.user_id"
                " JOIN rooms r ON r.id = b.room_id"
                " LEFT JOIN email_log e ON e.dedupe_key = 'reminder:' || b.id"
                " WHERE b.status = 'confirmed' AND b.start_at > ?"
                " AND b.start_at <= ? AND e.id IS NULL",
                (started, window_end),
            )
        )

        for item in due:
            # Re-check under a fresh read: the booking may have been
            # cancelled or preempted between the query above and now (spec
            # §12 D2 -- suppress if no longer confirmed).
            current = db.run_in_transaction(
                lambda conn, bid=item["booking_id"]: conn.query_one(
                    "SELECT status FROM bookings WHERE id = ?", (bid,)
                )
            )
            if current is None or current["status"] != models.CONFIRMED:
                continue

            event = EmailEvent(
                kind="E10",
                to_email=item["owner_email"],
                context={
                    "full_name": item["owner_name"],
                    "room_name": item["room_name"],
                    "title": item["title"],
                    "start_at": item["start_at"],
                    "end_at": item["end_at"],
                },
                related_booking_id=item["booking_id"],
                dedupe_key=f"reminder:{item['booking_id']}",
            )
            ids = enqueue(db, [event])
            if not ids:
                # Another concurrent run already claimed this dedupe key.
                continue

            row = db.run_in_transaction(
                lambda conn, rid=ids[0]: conn.query_one(
                    "SELECT * FROM email_log WHERE id = ?", (rid,)
                )
            )
            outcome = _send_row(db, row, transport, sent_today, cap, max_attempts)
            if outcome == "sent":
                report.sent += 1
            elif outcome == "skipped":
                report.skipped += 1
            else:
                report.failed += 1
    except Exception as exc:  # noqa: BLE001 - record the failure, then re-raise
        _record_cron_run(db, started, ok=False, detail=str(exc))
        raise

    _record_cron_run(
        db,
        started,
        ok=True,
        detail=f"sent={report.sent} skipped={report.skipped} failed={report.failed}",
    )
    return report


def run_admin_digest(db: Database, *, transport: Transport | None = None) -> int:
    """E7 batched admin digest (spec §9.4 / §12 D4). Returns emails sent."""
    started = now_utc()
    transport = transport or _default_transport()
    settings = db.run_in_transaction(Settings.load)
    cap = settings.daily_email_cap
    max_attempts = settings.email_max_attempts
    sent_today = [db.run_in_transaction(_count_sent_today)]

    admins = db.run_in_transaction(
        lambda conn: conn.query_all(
            "SELECT * FROM users WHERE is_admin = ? AND status = ?",
            (True, models.ACTIVE),
        )
    )
    pending = db.run_in_transaction(
        lambda conn: conn.query_all(
            "SELECT full_name, department, phone, email, created_at FROM users"
            " WHERE status = ? ORDER BY created_at",
            (models.PENDING_APPROVAL,),
        )
    )
    if not pending or not admins:
        return 0

    sent_count = 0
    for admin in admins:
        last = db.run_in_transaction(
            lambda conn, addr=admin["email"]: conn.query_one(
                "SELECT created_at FROM email_log WHERE to_email = ?"
                " AND type = 'E7' AND status = 'sent'"
                " ORDER BY created_at DESC LIMIT 1",
                (addr,),
            )
        )
        if last is not None and (started - last["created_at"]) < DIGEST_INTERVAL:
            continue  # already digested this admin within the last hour

        event = EmailEvent(
            kind="E7",
            to_email=admin["email"],
            context={
                "admin_name": admin["full_name"],
                "pending": [dict(p) for p in pending],
            },
        )
        ids = enqueue(db, [event])
        if not ids:
            continue
        row = db.run_in_transaction(
            lambda conn, rid=ids[0]: conn.query_one(
                "SELECT * FROM email_log WHERE id = ?", (rid,)
            )
        )
        if _send_row(db, row, transport, sent_today, cap, max_attempts) == "sent":
            sent_count += 1
    return sent_count


__all__ = [
    "EmailEvent",
    "SendReport",
    "ReminderReport",
    "enqueue",
    "send_pending",
    "resend",
    "can_resend",
    "run_reminders",
    "run_admin_digest",
    "set_default_transport",
    "MAX_ATTEMPTS",
]
