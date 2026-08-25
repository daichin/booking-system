"""Email service (spec §9). See CONTRACT.md §5 "Task 2" for the binding
interface: :class:`EmailEvent`, :func:`enqueue`, :func:`send_pending`,
:func:`run_reminders`, :func:`run_admin_digest`.

Design notes
------------
``email_log`` (spec §4.7) has no column for a rendered message body -- only
``subject`` for admin visibility. Two things follow from that:

1. The context needed to *render* a queued email (recipient name, booking
   time, one-time tokens embedded in a link, ...) is kept in an in-process
   cache (``_CONTEXT_CACHE``) keyed by the ``email_log`` row id, populated by
   :func:`enqueue` and consumed by :func:`send_pending`. This is sufficient
   for this deployment: the web process is long-lived (Render), and a queued
   row is normally flushed within the same request that created it. A row
   that outlives the process (a crash between enqueue and send) cannot be
   re-rendered and is surfaced as ``failed`` rather than silently lost.
2. One-time secrets (verification/invite/reset tokens) are only ever known
   to the caller that minted them, so they must already be embedded as full
   URLs in ``EmailEvent.context`` by the time it reaches :func:`enqueue`.

Retries never sleep in-process (spec instruction): a failed send increments
``email_log.attempts`` and leaves the row ``queued`` until three attempts
have been made, at which point it becomes ``failed``. A later call to
:func:`send_pending` -- from the next request, or the reminder cron -- is
what actually retries it.

Emails are only ever sent from outside a database transaction (CONTRACT.md
§3 rule 6): every write to ``email_log`` is its own short transaction, wrapped
tightly around the transport call rather than the caller's business
transaction.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app import models
from app.db.base import Connection, Database
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

#: Attempts before a failed send becomes terminal (spec §9.4: "retried up to
#: 3 times ... then marked failed").
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


# --- in-process render context cache ----------------------------------------

_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE: dict[str, EmailEvent] = {}


def _remember(row_id: str, event: EmailEvent) -> None:
    with _CACHE_LOCK:
        _CONTEXT_CACHE[row_id] = event


def _recall(row_id: str) -> EmailEvent | None:
    with _CACHE_LOCK:
        return _CONTEXT_CACHE.get(row_id)


def _forget(row_id: str) -> None:
    with _CACHE_LOCK:
        _CONTEXT_CACHE.pop(row_id, None)


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


def _attempt_send(
    db: Database, row: dict[str, Any], event: EmailEvent, transport: Transport
) -> bool | None:
    """Try to deliver one queued row. Returns ``True`` (sent), ``False``
    (exhausted retries, now ``failed``), or ``None`` (transient failure,
    still ``queued`` for a later call)."""
    rendered = email_templates.render(row["type"], event.context)
    message = Message(
        to_email=row["to_email"],
        to_name=event.context.get("full_name"),
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
    )
    result = transport.send(message)
    attempts = int(row["attempts"]) + 1

    if result.ok:
        _update_row(
            db,
            row["id"],
            status="sent",
            provider_message_id=result.message_id,
            error=None,
            attempts=attempts,
            sent_at=now_utc(),
        )
        _forget(row["id"])
        return True

    terminal = attempts >= MAX_ATTEMPTS
    _update_row(
        db,
        row["id"],
        status="failed" if terminal else "queued",
        error=result.error,
        attempts=attempts,
    )
    if terminal:
        _forget(row["id"])
        return False
    return None


def _send_row(
    db: Database,
    row: dict[str, Any],
    transport: Transport,
    sent_today: list[int],
    cap: int,
) -> str:
    """Deliver (or skip/fail) one queued row. Returns "sent"/"failed"/"skipped"."""
    event = _recall(row["id"])
    if event is None:
        _update_row(db, row["id"], status="failed", error="context_unavailable")
        return "failed"

    if row["type"] not in _CRITICAL_KINDS and sent_today[0] >= cap:
        _update_row(db, row["id"], status="skipped", error="daily_cap_reached")
        _forget(row["id"])
        return "skipped"

    outcome = _attempt_send(db, row, event, transport)
    if outcome is True:
        sent_today[0] += 1
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
            subject = email_templates.render(event.kind, event.context).subject
            try:
                conn.execute(
                    "INSERT INTO email_log (id, to_email, type, subject, status,"
                    " related_booking_id, dedupe_key, attempts, created_at)"
                    " VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?)",
                    (
                        row_id,
                        event.to_email,
                        event.kind,
                        subject,
                        event.related_booking_id,
                        event.dedupe_key,
                        now_utc(),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - narrowed immediately below
                if event.dedupe_key and _is_unique_violation(exc):
                    results.append(None)
                    continue
                raise
            results.append(row_id)
        return results

    results = db.run_in_transaction(work)

    ids: list[str] = []
    for event, row_id in zip(events, results):
        if row_id is None:
            continue
        _remember(row_id, event)
        ids.append(row_id)
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
        outcome = _send_row(db, row, transport, sent_today, cap)
        if outcome == "sent":
            report.sent += 1
        elif outcome == "skipped":
            report.skipped += 1
        else:
            report.failed += 1
    return report


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
            outcome = _send_row(db, row, transport, sent_today, cap)
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
        if _send_row(db, row, transport, sent_today, cap) == "sent":
            sent_count += 1
    return sent_count


__all__ = [
    "EmailEvent",
    "SendReport",
    "ReminderReport",
    "enqueue",
    "send_pending",
    "run_reminders",
    "run_admin_digest",
    "set_default_transport",
    "MAX_ATTEMPTS",
]
