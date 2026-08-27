"""Admin console (spec §6.6, §8, §6.7, §9.4, §10.4).

Every route here is admin-only. :func:`_guard` is the gate every handler
calls first: it checks login *before* admin status, so an anonymous visitor
is bounced to ``/login`` (the framework's usual behaviour for
``NOT_AUTHENTICATED``) while a signed-in non-admin gets a plain 403 with
``NOT_ADMIN`` -- calling :func:`app.web.framework.require_admin` alone would
have collapsed both cases into a 403, since it treats "no user" and "wrong
role" identically.

Screens: dashboard, approvals queue, members, invitations, rooms (CRUD +
deactivation confirmation), all bookings, preemption log, settings (every
key in spec §5), email log, and the audit trail. Bookings, preemptions, and
audit all accept ``?format=csv`` (spec §6.7).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Iterable
from urllib.parse import urlencode

from app import models
from app.errors import AppError, CONFIRMATION_REQUIRED
from app.i18n import error_message, t
from app.services import accounts, audit, mailer, preemption, rooms
from app.services import bookings as bookings_service
from app.services.mailer import enqueue as mailer_enqueue
from app.settings import DEFAULTS as SETTINGS_DEFAULTS
from app.settings import Settings
from app.settings import update as settings_update
from app.timeutil import format_hhmm, local_date, now_utc, taipei_midnight, to_taipei
from app.web import html
from app.web.framework import (
    CSRF_FIELD,
    csrf_token,
    Request,
    Response,
    Router,
    require_admin,
    require_login,
)
from app.web.layout import page

#: Numeric settings, in the order they are shown (spec §5). ``quota_by_level``
#: is handled separately because it renders as ten fields, not one.
_SETTINGS_ORDER = [
    "slot_minutes",
    "max_booking_minutes",
    "booking_horizon_days",
    "default_open_time",
    "default_close_time",
    "preemption_protection_minutes",
    "reminder_lead_minutes",
    "reminders_enabled",
    "verify_token_hours",
    "invite_token_hours",
    "reset_token_hours",
    "daily_email_cap",
]

#: A GitHub Actions schedule fires every 15 minutes (spec §9.3); flag the
#: dashboard once it has been silent noticeably longer than that, since
#: schedules are known to drift under load or auto-disable after 60 days of
#: repo inactivity.
_CRON_STALE_MINUTES = 60

_ADMIN_NAV = (
    ("/admin", "admin.nav.dashboard"),
    ("/admin/approvals", "admin.nav.approvals"),
    ("/admin/members", "admin.nav.members"),
    ("/admin/invitations", "admin.nav.invitations"),
    ("/admin/rooms", "admin.nav.rooms"),
    ("/admin/bookings", "admin.nav.bookings"),
    ("/admin/preemptions", "admin.nav.preemptions"),
    ("/admin/settings", "admin.nav.settings"),
    ("/admin/emails", "admin.nav.emails"),
    ("/admin/audit", "admin.nav.audit"),
)


# --- small shared helpers ----------------------------------------------------


def _guard(request: Request) -> models.User:
    """Require a signed-in admin. See the module docstring for why login is
    checked before role."""
    require_login(request)
    require_admin(request)
    assert request.user is not None
    return request.user


def _csrf(request: Request) -> html.Markup:
    return html.hidden(CSRF_FIELD, csrf_token(request))


def _qs(params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return f"?{urlencode(clean)}" if clean else ""


def _flash(request: Request) -> html.Markup | None:
    err = request.query.get("err")
    if err:
        return html.notice(error_message(err), kind="error")
    msg = request.query.get("msg")
    if msg:
        return html.notice(t(f"admin.flash.{msg}"), kind="success")
    return None


def _admin_nav(request: Request) -> html.Markup:
    items = [
        html.li(
            html.a(
                t(label_key),
                href=href,
                aria_current="page" if request.path == href else None,
            )
        )
        for href, label_key in _ADMIN_NAV
    ]
    return html.nav(html.ul(*items), class_="site-nav")


def _shell(request: Request, title: str, *content: Any) -> Response:
    banner = _flash(request)
    return Response.html(
        page(
            request,
            title,
            _admin_nav(request),
            *content,
            banners=[banner] if banner else None,
        )
    )


def _status_label(status: str) -> str:
    return t(f"status.{status}")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    return f"{to_taipei(value):%Y-%m-%d %H:%M}"


def _csv_response(filename: str, header: Iterable[str], rows: Iterable[Iterable[Any]]) -> Response:
    """A CSV download (spec §6.7). UTF-8 with a BOM so Excel shows Chinese
    text correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    body = ("﻿" + buffer.getvalue()).encode("utf-8")
    response = Response(body, 200, "text/csv; charset=utf-8")
    response.headers.append(
        ("Content-Disposition", f'attachment; filename="{filename}"')
    )
    return response


def _wants_csv(request: Request) -> bool:
    return request.query.get("format") == "csv"


# =============================================================================
# Dashboard
# =============================================================================


def _dashboard(request: Request) -> Response:
    actor = _guard(request)
    db = request.db

    def work(conn):
        pending = conn.query_value(
            "SELECT COUNT(*) FROM users WHERE status = ?", (models.PENDING_APPROVAL,)
        )
        settings = Settings.load(conn)
        # Same "Taipei calendar day" boundary the mailer's cap guard uses,
        # computed directly here since that helper is private to
        # app.services.mailer.
        day_start = taipei_midnight(local_date(now_utc()))
        sent_today = conn.query_value(
            "SELECT COUNT(*) FROM email_log WHERE status = 'sent' AND created_at >= ?",
            (day_start,),
        )
        last_run = conn.query_one(
            "SELECT * FROM cron_runs WHERE job = 'send_reminders'"
            " ORDER BY started_at DESC LIMIT 1"
        )
        failures = conn.query_all(
            "SELECT * FROM email_log WHERE status = 'failed'"
            " ORDER BY created_at DESC LIMIT 20"
        )
        return int(pending or 0), settings, int(sent_today or 0), last_run, failures

    pending, settings, sent_today, last_run, failures = db.run_in_transaction(work)

    pending_panel = html.div(
        html.h2(t("admin.dashboard.pending_approvals")),
        html.p(t("admin.dashboard.pending_approvals_help", count=pending)),
        html.p(html.a(t("admin.dashboard.go_approvals"), href="/admin/approvals", class_="btn")),
        class_="panel",
    )

    emails_panel = html.div(
        html.h2(t("admin.dashboard.emails_today")),
        html.p(
            t(
                "admin.dashboard.emails_today_value",
                sent=sent_today,
                cap=settings.daily_email_cap,
            )
        ),
        class_="panel",
    )

    if last_run is None:
        cron_body = html.notice(t("admin.dashboard.cron_never"), kind="warning")
    else:
        when = last_run["finished_at"] or last_run["started_at"]
        elapsed_minutes = (now_utc() - when).total_seconds() / 60
        status_text = (
            t("admin.dashboard.cron_status_ok")
            if last_run["ok"]
            else t("admin.dashboard.cron_status_failed")
        )
        ok_line = html.p(
            t("admin.dashboard.cron_ok", when=_fmt(when), status=status_text)
        )
        if elapsed_minutes > _CRON_STALE_MINUTES or not last_run["ok"]:
            cron_body = html.join(
                [
                    ok_line,
                    html.notice(
                        t("admin.dashboard.cron_stale", minutes=_CRON_STALE_MINUTES),
                        kind="warning",
                    ),
                ]
            )
        else:
            cron_body = ok_line

    cron_panel = html.div(html.h2(t("admin.dashboard.cron_title")), cron_body, class_="panel")

    if failures:
        rows = [
            html.tr(
                html.td(_fmt(row["created_at"])),
                html.td(row["type"]),
                html.td(row["to_email"]),
                html.td(row["error"] or ""),
            )
            for row in failures
        ]
        failures_body = html.div(
            html.table(html.tbody(*rows)), class_="table-wrap"
        )
    else:
        failures_body = html.p(t("admin.dashboard.no_failures"), class_="muted")

    failures_panel = html.div(
        html.h2(t("admin.dashboard.recent_failures")), failures_body, class_="panel"
    )

    return _shell(
        request,
        t("admin.dashboard.title"),
        pending_panel,
        emails_panel,
        cron_panel,
        failures_panel,
    )


# =============================================================================
# Approvals
# =============================================================================


def _approvals_list(request: Request) -> Response:
    _guard(request)
    rows = request.db.run_in_transaction(
        lambda conn: conn.query_all(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at",
            (models.PENDING_APPROVAL,),
        )
    )

    if not rows:
        body = html.p(t("admin.approvals.empty"), class_="muted")
    else:
        table_rows = []
        for row in rows:
            actions = html.div(
                html.form(
                    _csrf(request),
                    html.button(t("admin.approvals.approve"), type="submit"),
                    method="post",
                    action=f"/admin/approvals/{row['id']}/approve",
                    class_="inline-form",
                ),
                html.form(
                    _csrf(request),
                    html.button(
                        t("admin.approvals.reject"), type="submit", class_="danger"
                    ),
                    method="post",
                    action=f"/admin/approvals/{row['id']}/reject",
                    class_="inline-form",
                ),
                class_="actions",
            )
            table_rows.append(
                html.tr(
                    html.td(row["full_name"]),
                    html.td(row["department"]),
                    html.td(row["phone"]),
                    html.td(row["email"]),
                    html.td(_fmt(row["created_at"])),
                    html.td(actions),
                )
            )
        body = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.approvals.col_name")),
                        html.th(t("admin.approvals.col_department")),
                        html.th(t("admin.approvals.col_phone")),
                        html.th(t("admin.approvals.col_email")),
                        html.th(t("admin.approvals.col_registered_at")),
                        html.th(t("admin.approvals.col_actions")),
                    )
                ),
                html.tbody(*table_rows),
            ),
            class_="table-wrap",
        )

    return _shell(request, t("admin.approvals.title"), html.div(body, class_="panel"))


def _approvals_approve(request: Request) -> Response:
    actor = _guard(request)
    try:
        accounts.approve(request.db, actor, request.params["user_id"])
    except AppError as exc:
        return Response.redirect(f"/admin/approvals{_qs({'err': exc.code})}")
    return Response.redirect(f"/admin/approvals{_qs({'msg': 'approved'})}")


def _approvals_reject(request: Request) -> Response:
    actor = _guard(request)
    try:
        accounts.reject(request.db, actor, request.params["user_id"])
    except AppError as exc:
        return Response.redirect(f"/admin/approvals{_qs({'err': exc.code})}")
    return Response.redirect(f"/admin/approvals{_qs({'msg': 'rejected'})}")


# =============================================================================
# Members
# =============================================================================


def _member_filters(request: Request) -> dict[str, str]:
    return {
        "q": request.query.get("q", "").strip(),
        "status": request.query.get("status", "").strip(),
        "level": request.query.get("level", "").strip(),
    }


def _query_members(request: Request, filters: dict[str, str]) -> list[dict[str, Any]]:
    def work(conn):
        clauses: list[str] = []
        params: list[Any] = []
        if filters["q"]:
            like = f"%{filters['q'].lower()}%"
            clauses.append("(LOWER(full_name) LIKE ? OR LOWER(email) LIKE ?)")
            params.extend([like, like])
        if filters["status"]:
            clauses.append("status = ?")
            params.append(filters["status"])
        if filters["level"]:
            clauses.append("level = ?")
            params.append(int(filters["level"]))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return conn.query_all(
            f"SELECT * FROM users{where} ORDER BY created_at DESC LIMIT 500",
            tuple(params),
        )

    return request.db.run_in_transaction(work)


def _members_list(request: Request) -> Response:
    _guard(request)
    filters = _member_filters(request)
    rows = _query_members(request, filters)

    status_options = [html.option(t("admin.members.status_any"), value="")]
    for status in models.USER_STATUSES:
        status_options.append(
            html.option(
                _status_label(status),
                value=status,
                selected=status == filters["status"] or None,
            )
        )
    level_options = [html.option(t("admin.members.level_any"), value="")]
    for level in range(models.MIN_LEVEL, models.MAX_LEVEL + 1):
        level_options.append(
            html.option(
                str(level),
                value=str(level),
                selected=str(level) == filters["level"] or None,
            )
        )

    filter_form = html.form(
        html.div(
            html.div(
                html.label(t("admin.members.search_label"), for_="f-q"),
                html.input_(type="text", name="q", id="f-q", value=filters["q"]),
                class_="field",
            ),
            html.div(
                html.label(t("admin.members.status_label"), for_="f-status"),
                html.select(*status_options, name="status", id="f-status"),
                class_="field",
            ),
            html.div(
                html.label(t("admin.members.level_label"), for_="f-level"),
                html.select(*level_options, name="level", id="f-level"),
                class_="field",
            ),
            class_="grid-2",
        ),
        html.button(t("common.search"), type="submit"),
        method="get",
        action="/admin/members",
    )

    if not rows:
        table = html.p(t("admin.members.empty"), class_="muted")
    else:
        table_rows = []
        for row in rows:
            hidden_filters = [
                html.hidden("q", filters["q"]),
                html.hidden("status", filters["status"]),
                html.hidden("level", filters["level"]),
            ]
            level_select = html.select(
                *[
                    html.option(
                        str(level_num),
                        value=str(level_num),
                        selected=level_num == int(row["level"]) or None,
                    )
                    for level_num in range(models.MIN_LEVEL, models.MAX_LEVEL + 1)
                ],
                # Deliberately not "level": the row carries a hidden "level"
                # filter so the list keeps its filtering after the redirect,
                # and two fields of the same name made the browser submit
                # both. The parser keeps the first, which was the empty
                # filter, so every level change failed as out of range.
                name="new_level",
            )
            level_form = html.form(
                _csrf(request),
                *hidden_filters,
                level_select,
                html.button(t("admin.members.update_level"), type="submit"),
                method="post",
                action=f"/admin/members/{row['id']}/level",
                class_="inline-form",
            )
            if row["status"] == models.SUSPENDED:
                toggle_form = html.form(
                    _csrf(request),
                    *hidden_filters,
                    html.button(t("admin.members.reactivate"), type="submit"),
                    method="post",
                    action=f"/admin/members/{row['id']}/reactivate",
                    class_="inline-form",
                )
            elif row["status"] == models.ACTIVE:
                toggle_form = html.form(
                    _csrf(request),
                    *hidden_filters,
                    html.button(
                        t("admin.members.suspend"), type="submit", class_="danger"
                    ),
                    method="post",
                    action=f"/admin/members/{row['id']}/suspend",
                    class_="inline-form",
                )
            else:
                toggle_form = ""

            # Deleting yourself always costs your own password, so an admin
            # deleting their own account goes through the member screen where
            # they can type it. The admin route has no password to offer and
            # would only report the credentials as wrong.
            delete_link = html.a(
                t("admin.members.delete"),
                href=(
                    "/account"
                    if request.user is not None and row["id"] == request.user.id
                    else f"/admin/members/{row['id']}/delete{_qs(filters)}"
                ),
                class_="btn danger",
            )

            if row["deleted_at"] is not None:
                # A tombstone has nothing left to administer: its level,
                # suspension and deletion are all meaningless now. The row
                # stays visible so the admin can see what became of it.
                status_cell = html.span(
                    t("admin.members.deleted_tag"), class_="tag tag-cancelled"
                )
                actions = html.span("-", class_="muted")
            else:
                status_cell = _status_label(row["status"])
                actions = html.div(level_form, toggle_form, delete_link,
                                   class_="actions")

            table_rows.append(
                html.tr(
                    html.td(row["full_name"]),
                    html.td(row["email"]),
                    html.td(row["department"]),
                    html.td(str(row["level"])),
                    html.td(status_cell),
                    html.td(_fmt(row["created_at"])),
                    html.td(actions),
                )
            )
        table = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.members.col_name")),
                        html.th(t("admin.members.col_email")),
                        html.th(t("admin.members.col_department")),
                        html.th(t("admin.members.col_level")),
                        html.th(t("admin.members.col_status")),
                        html.th(t("admin.members.col_joined")),
                        html.th(t("admin.members.col_actions")),
                    )
                ),
                html.tbody(*table_rows),
            ),
            class_="table-wrap",
        )

    return _shell(
        request,
        t("admin.members.title"),
        html.div(filter_form, class_="panel"),
        html.div(table, class_="panel"),
    )


def _members_redirect(request: Request, **extra: str) -> Response:
    params = _member_filters(request)
    params.update({k: v for k, v in extra.items() if v})
    return Response.redirect(f"/admin/members{_qs(params)}")


def _members_set_level(request: Request) -> Response:
    actor = _guard(request)
    try:
        level = int(request.form.get("new_level", ""))
        accounts.set_level(request.db, actor, request.params["user_id"], level)
    except (ValueError, AppError) as exc:
        code = exc.code if isinstance(exc, AppError) else "INVALID_LEVEL"
        return _members_redirect(request, err=code)
    return _members_redirect(request, msg="level_changed")


def _members_delete_confirm(request: Request) -> Response:
    """Ask before destroying something no screen can undo.

    An admin has no password of the member's to type, so a page that spells
    out what goes and what stays is the confirmation step. It is a GET, so
    landing here by mistake changes nothing.
    """
    actor = _guard(request)
    user_id = request.params["user_id"]
    if user_id == actor.id:
        return Response.redirect("/account")
    row = request.db.run_in_transaction(
        lambda conn: conn.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    )
    if row is None or row["deleted_at"] is not None:
        return _members_redirect(request, err="USER_NOT_FOUND")

    filters = _member_filters(request)
    return _shell(
        request,
        t("admin.members.delete.title"),
        html.div(
            html.p(
                t(
                    "admin.members.delete.confirm",
                    name=row["full_name"],
                    email=row["email"],
                )
            ),
            html.p(t("admin.members.delete.explain")),
            html.p(t("admin.members.delete.keeps_history"), class_="muted"),
            html.form(
                _csrf(request),
                *[html.hidden(k, v) for k, v in filters.items()],
                html.div(
                    html.button(
                        t("admin.members.delete.submit"),
                        type="submit",
                        class_="danger",
                    ),
                    html.a(
                        t("admin.members.delete.cancel"),
                        href=f"/admin/members{_qs(filters)}",
                        class_="btn secondary",
                    ),
                    class_="actions",
                ),
                method="post",
                action=f"/admin/members/{user_id}/delete",
            ),
            class_="panel danger-zone",
        ),
    )


def _members_delete(request: Request) -> Response:
    actor = _guard(request)
    if request.params["user_id"] == actor.id:
        return Response.redirect("/account")
    try:
        accounts.delete_account(
            request.db, actor=actor, user_id=request.params["user_id"]
        )
    except AppError as exc:
        return _members_redirect(request, err=exc.code)
    return _members_redirect(request, msg="account_deleted")


def _members_suspend(request: Request) -> Response:
    actor = _guard(request)
    try:
        accounts.set_suspended(request.db, actor, request.params["user_id"], True)
    except AppError as exc:
        return _members_redirect(request, err=exc.code)
    return _members_redirect(request, msg="suspended")


def _members_reactivate(request: Request) -> Response:
    actor = _guard(request)
    try:
        accounts.set_suspended(request.db, actor, request.params["user_id"], False)
    except AppError as exc:
        return _members_redirect(request, err=exc.code)
    return _members_redirect(request, msg="reactivated")


# =============================================================================
# Invitations
# =============================================================================


def _outstanding_invitations(request: Request) -> list[dict[str, Any]]:
    return request.db.run_in_transaction(
        lambda conn: conn.query_all(
            "SELECT et.*, u.full_name AS created_by_name FROM email_tokens et"
            " LEFT JOIN users u ON u.id = et.created_by"
            " WHERE et.type = ? AND et.used_at IS NULL AND et.revoked_at IS NULL"
            " ORDER BY et.created_at DESC",
            (models.INVITE,),
        )
    )


def _invitations_table(request: Request) -> html.Markup:
    rows = _outstanding_invitations(request)
    if not rows:
        return html.p(t("admin.invitations.empty"), class_="muted")

    now = now_utc()
    table_rows = []
    for row in rows:
        status = (
            t("admin.invitations.status_expired")
            if row["expires_at"] <= now
            else t("admin.invitations.status_valid")
        )
        revoke_form = html.form(
            _csrf(request),
            html.button(t("admin.invitations.revoke"), type="submit", class_="danger"),
            method="post",
            action=f"/admin/invitations/{row['id']}/revoke",
            class_="inline-form",
        )
        table_rows.append(
            html.tr(
                html.td(row["email"]),
                html.td(str(row["invited_level"] or models.MIN_LEVEL)),
                html.td(row["created_by_name"] or "-"),
                html.td(_fmt(row["created_at"])),
                html.td(_fmt(row["expires_at"])),
                html.td(status),
                html.td(revoke_form),
            )
        )
    return html.div(
        html.table(
            html.thead(
                html.tr(
                    html.th(t("admin.invitations.col_email")),
                    html.th(t("admin.invitations.col_level")),
                    html.th(t("admin.invitations.col_invited_by")),
                    html.th(t("admin.invitations.col_created_at")),
                    html.th(t("admin.invitations.col_expires_at")),
                    html.th(t("admin.invitations.col_status")),
                    html.th(t("admin.invitations.col_actions")),
                )
            ),
            html.tbody(*table_rows),
        ),
        class_="table-wrap",
    )


def _invitations_form(request: Request) -> html.Markup:
    return html.form(
        # Same omission as the create-room form had; both are now covered
        # by tests/test_csrf_coverage.py.
        _csrf(request),
        html.div(
            html.label(t("admin.invitations.emails_label"), for_="f-emails"),
            html.textarea(
                "", name="emails", id="f-emails", rows="4", required=True
            ),
            class_="field",
        ),
        html.div(
            html.label(t("admin.invitations.level_label"), for_="f-level"),
            html.input_(
                type="number",
                name="level",
                id="f-level",
                min=str(models.MIN_LEVEL),
                max=str(models.MAX_LEVEL),
                required=False,
            ),
            class_="field",
        ),
        html.button(t("admin.invitations.send"), type="submit"),
        method="post",
        action="/admin/invitations",
    )


def _invitations_list(request: Request) -> Response:
    _guard(request)
    return _shell(
        request,
        t("admin.invitations.title"),
        html.div(_invitations_form(request), class_="panel"),
        html.div(
            html.h2(t("admin.invitations.outstanding_title")),
            _invitations_table(request),
            class_="panel",
        ),
    )


def _invitations_send(request: Request) -> Response:
    actor = _guard(request)
    raw_emails = request.form.get("emails", "")
    addresses = [
        part.strip()
        for chunk in raw_emails.replace(",", "\n").splitlines()
        for part in [chunk.strip()]
        if part
    ]
    level_raw = request.form.get("level", "").strip()
    level = int(level_raw) if level_raw else None

    result_body: html.Markup
    if not addresses:
        result_body = html.notice(error_message("MISSING_FIELD", field="emails"), kind="error")
    else:
        try:
            results = accounts.invite(request.db, actor, addresses, level)
        except AppError as exc:
            result_body = html.notice(error_message(exc.code), kind="error")
        else:
            outgoing = []
            lines = []
            for result in results:
                if result.ok:
                    outgoing.extend(result.emails)
                    lines.append(
                        html.li(t("admin.invitations.result_ok", email=result.email))
                    )
                else:
                    lines.append(
                        html.li(
                            t(
                                "admin.invitations.result_fail",
                                email=result.email,
                                reason=error_message(result.error),
                            )
                        )
                    )
            if outgoing:
                mailer_enqueue(request.db, outgoing)
            result_body = html.div(
                html.h2(t("admin.invitations.result_title")), html.ul(*lines)
            )

    return _shell(
        request,
        t("admin.invitations.title"),
        html.div(result_body, class_="panel"),
        html.div(_invitations_form(request), class_="panel"),
        html.div(
            html.h2(t("admin.invitations.outstanding_title")),
            _invitations_table(request),
            class_="panel",
        ),
    )


def _invitations_revoke(request: Request) -> Response:
    actor = _guard(request)
    try:
        accounts.revoke_invitation(request.db, actor, request.params["token_id"])
    except AppError as exc:
        return Response.redirect(f"/admin/invitations{_qs({'err': exc.code})}")
    return Response.redirect(f"/admin/invitations{_qs({'msg': 'revoked'})}")


# =============================================================================
# Rooms
# =============================================================================


def _room_form_fields(request: Request) -> dict[str, Any]:
    form = request.form
    return {
        "name": form.get("name", ""),
        "capacity": form.get("capacity", ""),
        "location": form.get("location", ""),
        "equipment_note": form.get("equipment_note", ""),
        "open_time": form.get("open_time", ""),
        "close_time": form.get("close_time", ""),
    }


def _room_row(request: Request, room: models.Room) -> html.Markup:
    open_value = format_hhmm(room.open_minutes) if room.open_minutes is not None else ""
    close_value = format_hhmm(room.close_minutes) if room.close_minutes is not None else ""
    edit_form = html.form(
        _csrf(request),
        html.div(
            html.label(t("admin.rooms.field_name"), for_=f"name-{room.id}"),
            html.input_(type="text", name="name", id=f"name-{room.id}", value=room.name),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_capacity"), for_=f"cap-{room.id}"),
            html.input_(
                type="number",
                name="capacity",
                id=f"cap-{room.id}",
                value=room.capacity,
                required=False,
            ),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_location"), for_=f"loc-{room.id}"),
            html.input_(
                type="text",
                name="location",
                id=f"loc-{room.id}",
                value=room.location or "",
                required=False,
            ),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_equipment"), for_=f"equip-{room.id}"),
            html.input_(
                type="text",
                name="equipment_note",
                id=f"equip-{room.id}",
                value=room.equipment_note or "",
                required=False,
            ),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_open_time"), for_=f"open-{room.id}"),
            html.input_(
                type="time", name="open_time", id=f"open-{room.id}",
                value=open_value, required=False,
            ),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_close_time"), for_=f"close-{room.id}"),
            html.input_(
                type="time", name="close_time", id=f"close-{room.id}",
                value=close_value, required=False,
            ),
            class_="field",
        ),
        html.button(t("admin.rooms.save"), type="submit"),
        method="post",
        action=f"/admin/rooms/{room.id}",
    )

    if room.is_active:
        toggle = html.form(
            _csrf(request),
            html.button(t("admin.rooms.deactivate"), type="submit", class_="danger"),
            method="post",
            action=f"/admin/rooms/{room.id}/deactivate",
            class_="inline-form",
        )
    else:
        toggle = html.form(
            _csrf(request),
            html.button(t("admin.rooms.activate"), type="submit"),
            method="post",
            action=f"/admin/rooms/{room.id}/activate",
            class_="inline-form",
        )

    hours = (
        f"{open_value}–{close_value}"
        if open_value and close_value
        else t("admin.rooms.default_hours")
    )
    status_label = (
        t("admin.rooms.status_active") if room.is_active else t("admin.rooms.status_inactive")
    )

    details = html.details(
        html.summary(t("admin.rooms.edit")),
        edit_form,
    )

    return html.tr(
        html.td(room.name),
        html.td(str(room.capacity) if room.capacity is not None else "-"),
        html.td(room.location or "-"),
        html.td(room.equipment_note or "-"),
        html.td(hours),
        html.td(status_label),
        html.td(html.div(details, toggle, class_="stack")),
    )


def _rooms_create_form(request: Request) -> html.Markup:
    return html.form(
        # Every POST form needs this; without it the request is refused as
        # cross-site. tests/test_csrf_coverage.py checks all of them.
        _csrf(request),
        html.div(
            html.label(t("admin.rooms.field_name"), for_="new-name"),
            html.input_(type="text", name="name", id="new-name"),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_capacity"), for_="new-cap"),
            html.input_(type="number", name="capacity", id="new-cap", required=False),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_location"), for_="new-loc"),
            html.input_(type="text", name="location", id="new-loc", required=False),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_equipment"), for_="new-equip"),
            html.input_(type="text", name="equipment_note", id="new-equip", required=False),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_open_time"), for_="new-open"),
            html.input_(type="time", name="open_time", id="new-open", required=False),
            class_="field",
        ),
        html.div(
            html.label(t("admin.rooms.field_close_time"), for_="new-close"),
            html.input_(type="time", name="close_time", id="new-close", required=False),
            class_="field",
        ),
        html.button(t("admin.rooms.create"), type="submit"),
        method="post",
        action="/admin/rooms",
    )


def _rooms_list(request: Request) -> Response:
    _guard(request)
    room_list = rooms.list_rooms(request.db, include_inactive=True)
    if not room_list:
        table = html.p(t("admin.rooms.empty"), class_="muted")
    else:
        table = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.rooms.col_name")),
                        html.th(t("admin.rooms.col_capacity")),
                        html.th(t("admin.rooms.col_location")),
                        html.th(t("admin.rooms.col_equipment")),
                        html.th(t("admin.rooms.col_hours")),
                        html.th(t("admin.rooms.col_status")),
                        html.th(t("admin.rooms.col_actions")),
                    )
                ),
                html.tbody(*[_room_row(request, room) for room in room_list]),
            ),
            class_="table-wrap",
        )

    return _shell(
        request,
        t("admin.rooms.title"),
        html.div(
            html.h2(t("admin.rooms.create_title")),
            _rooms_create_form(request),
            class_="panel",
        ),
        html.div(table, class_="panel"),
    )


def _rooms_create(request: Request) -> Response:
    actor = _guard(request)
    fields = _room_form_fields(request)
    try:
        rooms.create_room(request.db, actor, **fields)
    except AppError as exc:
        return Response.redirect(f"/admin/rooms{_qs({'err': exc.code})}")
    return Response.redirect(f"/admin/rooms{_qs({'msg': 'room_created'})}")


def _rooms_update(request: Request) -> Response:
    actor = _guard(request)
    fields = _room_form_fields(request)
    try:
        rooms.update_room(request.db, actor, request.params["room_id"], **fields)
    except AppError as exc:
        return Response.redirect(f"/admin/rooms{_qs({'err': exc.code})}")
    return Response.redirect(f"/admin/rooms{_qs({'msg': 'room_updated'})}")


def _rooms_deactivate(request: Request) -> Response:
    actor = _guard(request)
    room_id = request.params["room_id"]
    confirmed = request.form.get("confirm_cancel") == "1"
    try:
        result = rooms.set_active(
            request.db, actor, room_id, False, cancel_bookings=confirmed
        )
    except AppError as exc:
        if exc.code == CONFIRMATION_REQUIRED and not confirmed:
            room = request.db.run_in_transaction(lambda conn: rooms.get_room(conn, room_id))
            count = exc.details.get("future_bookings", 0)
            return _shell(
                request,
                t("admin.rooms.confirm_title"),
                html.div(
                    html.h2(t("admin.rooms.confirm_title")),
                    html.p(
                        t("admin.rooms.confirm_body", name=room.name, count=count)
                    ),
                    html.div(
                        html.form(
                            _csrf(request),
                            html.hidden("confirm_cancel", "1"),
                            html.button(
                                t("admin.rooms.confirm_button"),
                                type="submit",
                                class_="danger",
                            ),
                            method="post",
                            action=f"/admin/rooms/{room_id}/deactivate",
                            class_="inline-form",
                        ),
                        html.a(t("common.cancel"), href="/admin/rooms", class_="btn secondary"),
                        class_="actions",
                    ),
                    class_="confirm-panel",
                ),
            )
        return Response.redirect(f"/admin/rooms{_qs({'err': exc.code})}")

    if result.emails:
        mailer_enqueue(request.db, result.emails)
    return Response.redirect(f"/admin/rooms{_qs({'msg': 'room_deactivated'})}")


def _rooms_activate(request: Request) -> Response:
    actor = _guard(request)
    try:
        rooms.set_active(request.db, actor, request.params["room_id"], True)
    except AppError as exc:
        return Response.redirect(f"/admin/rooms{_qs({'err': exc.code})}")
    return Response.redirect(f"/admin/rooms{_qs({'msg': 'room_activated'})}")


# =============================================================================
# Bookings
# =============================================================================


def _booking_filters(request: Request) -> dict[str, str]:
    return {
        "room_id": request.query.get("room_id", "").strip(),
        "date": request.query.get("date", "").strip(),
        "user_query": request.query.get("user_query", "").strip(),
    }


def _query_bookings(request: Request, filters: dict[str, str]) -> list[dict[str, Any]]:
    day = None
    if filters["date"]:
        try:
            day = date.fromisoformat(filters["date"])
        except ValueError:
            day = None
    rows = bookings_service.list_all(
        request.db, room_id=filters["room_id"] or None, day=day, limit=1000
    )
    needle = filters["user_query"].lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle in (row["owner_name"] or "").lower()
            or needle in (row["owner_email"] or "").lower()
        ]
    return rows


def _booking_status_label(status: str) -> str:
    return t(f"booking_status.{status}")


def _bookings_list(request: Request) -> Response:
    _guard(request)
    filters = _booking_filters(request)
    rows = _query_bookings(request, filters)

    if _wants_csv(request):
        return _csv_response(
            "bookings.csv",
            [
                t("admin.bookings.csv_room"),
                t("admin.bookings.csv_owner_name"),
                t("admin.bookings.csv_owner_email"),
                t("admin.bookings.csv_title"),
                t("admin.bookings.csv_start"),
                t("admin.bookings.csv_end"),
                t("admin.bookings.csv_status"),
            ],
            (
                [
                    row["room_name"],
                    row["owner_name"],
                    row["owner_email"],
                    row["title"],
                    _fmt(row["start_at"]),
                    _fmt(row["end_at"]),
                    _booking_status_label(row["status"]),
                ]
                for row in rows
            ),
        )

    room_options = [html.option(t("admin.bookings.filter_room_any"), value="")]
    for room in rooms.list_rooms(request.db, include_inactive=True):
        room_options.append(
            html.option(room.name, value=room.id, selected=room.id == filters["room_id"] or None)
        )

    filter_form = html.form(
        html.div(
            html.div(
                html.label(t("admin.bookings.filter_room"), for_="f-room"),
                html.select(*room_options, name="room_id", id="f-room"),
                class_="field",
            ),
            html.div(
                html.label(t("admin.bookings.filter_date"), for_="f-date"),
                html.input_(type="date", name="date", id="f-date", value=filters["date"]),
                class_="field",
            ),
            html.div(
                html.label(t("admin.bookings.filter_user"), for_="f-user"),
                html.input_(
                    type="text", name="user_query", id="f-user", value=filters["user_query"]
                ),
                class_="field",
            ),
            class_="grid-2",
        ),
        html.div(
            html.button(t("admin.bookings.filter_submit"), type="submit"),
            html.a(
                t("common.export_csv"),
                href=f"/admin/bookings{_qs({**filters, 'format': 'csv'})}",
                class_="btn secondary",
            ),
            class_="actions",
        ),
        method="get",
        action="/admin/bookings",
    )

    if not rows:
        table = html.p(t("admin.bookings.empty"), class_="muted")
    else:
        table_rows = []
        for row in rows:
            if row["status"] == models.CONFIRMED:
                cancel_form = html.form(
                    _csrf(request),
                    html.button(
                        t("admin.bookings.cancel"), type="submit", class_="danger"
                    ),
                    method="post",
                    action=f"/admin/bookings/{row['id']}/cancel",
                    class_="inline-form",
                )
            else:
                cancel_form = ""
            table_rows.append(
                html.tr(
                    html.td(row["room_name"]),
                    html.td(f"{row['owner_name']} ({row['owner_email']})"),
                    html.td(row["title"]),
                    html.td(_fmt(row["start_at"])),
                    html.td(_fmt(row["end_at"])),
                    html.td(
                        html.span(
                            _booking_status_label(row["status"]),
                            class_=f"tag tag-{row['status'].split('_')[0]}"
                            if row["status"] in (models.CONFIRMED, models.PREEMPTED)
                            else "tag tag-cancelled",
                        )
                    ),
                    html.td(cancel_form),
                )
            )
        table = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.bookings.col_room")),
                        html.th(t("admin.bookings.col_owner")),
                        html.th(t("admin.bookings.col_title")),
                        html.th(t("admin.bookings.col_start")),
                        html.th(t("admin.bookings.col_end")),
                        html.th(t("admin.bookings.col_status")),
                        html.th(t("admin.bookings.col_actions")),
                    )
                ),
                html.tbody(*table_rows),
            ),
            class_="table-wrap",
        )

    return _shell(
        request,
        t("admin.bookings.title"),
        html.div(filter_form, class_="panel"),
        html.div(table, class_="panel"),
    )


def _bookings_cancel(request: Request) -> Response:
    actor = _guard(request)
    try:
        result = bookings_service.cancel_booking(
            request.db, actor=actor, booking_id=request.params["booking_id"]
        )
    except AppError as exc:
        return Response.redirect(f"/admin/bookings{_qs({'err': exc.code})}")
    if result.emails:
        mailer_enqueue(request.db, result.emails)
    return Response.redirect(f"/admin/bookings{_qs({'msg': 'booking_cancelled'})}")


# =============================================================================
# Preemption log
# =============================================================================


def _preemptions_list(request: Request) -> Response:
    _guard(request)
    rows = preemption.log_entries(request.db, limit=1000)

    if _wants_csv(request):
        return _csv_response(
            "preemptions.csv",
            [
                t("admin.preemptions.csv_when"),
                t("admin.preemptions.csv_room"),
                t("admin.preemptions.csv_victim"),
                t("admin.preemptions.csv_victim_dept"),
                t("admin.preemptions.csv_victim_level"),
                t("admin.preemptions.csv_winner"),
                t("admin.preemptions.csv_winner_dept"),
                t("admin.preemptions.csv_winner_level"),
            ],
            (
                [
                    _fmt(row["occurred_at"]),
                    row["room_name"],
                    row["victim_name"],
                    row["victim_department"],
                    row["victim_level"],
                    row["winner_name"],
                    row["winner_department"],
                    row["winner_level"],
                ]
                for row in rows
            ),
        )

    if not rows:
        table = html.p(t("admin.preemptions.empty"), class_="muted")
    else:
        table_rows = [
            html.tr(
                html.td(_fmt(row["occurred_at"])),
                html.td(row["room_name"]),
                html.td(f"{row['victim_name']} ({row['victim_department']})"),
                html.td(str(row["victim_level"])),
                html.td(f"{row['winner_name']} ({row['winner_department']})"),
                html.td(str(row["winner_level"])),
            )
            for row in rows
        ]
        table = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.preemptions.col_when")),
                        html.th(t("admin.preemptions.col_room")),
                        html.th(t("admin.preemptions.col_victim")),
                        html.th(t("admin.preemptions.col_victim_level")),
                        html.th(t("admin.preemptions.col_winner")),
                        html.th(t("admin.preemptions.col_winner_level")),
                    )
                ),
                html.tbody(*table_rows),
            ),
            class_="table-wrap",
        )

    export_link = html.p(
        html.a(t("common.export_csv"), href="/admin/preemptions?format=csv", class_="btn secondary")
    )
    return _shell(request, t("admin.preemptions.title"), export_link, html.div(table, class_="panel"))


# =============================================================================
# Settings
# =============================================================================


def _setting_field(settings: Settings, key: str) -> html.Markup:
    label = t(f"admin.settings.{key}.label")
    help_text = t(f"admin.settings.{key}.help")
    value = settings.values[key]

    if key == "reminders_enabled":
        control = html.input_(
            type="checkbox", name=key, id=f"f-{key}", checked=bool(value) or None
        )
        row = html.div(
            html.label(control, html.span(label), class_="check"),
            html.small(help_text, class_="help"),
            class_="field",
        )
        return row

    if key in ("default_open_time", "default_close_time"):
        control = html.input_(type="time", name=key, id=f"f-{key}", value=str(value))
    else:
        control = html.input_(type="number", name=key, id=f"f-{key}", value=str(value))

    return html.div(
        html.label(label, for_=f"f-{key}"),
        control,
        html.small(help_text, class_="help"),
        class_="field",
    )


def _title_preset_field(settings: Settings) -> html.Markup:
    """The one-click meeting subjects offered on the booking form.

    Edited as one title per line, which is the least fiddly way to maintain a
    short list on a phone.
    """
    return html.div(
        html.h2(t("admin.settings.title_presets_title")),
        html.p(t("admin.settings.title_presets_help"), class_="muted"),
        html.div(
            html.label(t("admin.settings.title_presets_label"), for_="title-presets"),
            html.textarea(
                "\n".join(settings.title_presets),
                name="title_presets",
                id="title-presets",
                rows="7",
            ),
            class_="field",
        ),
        class_="panel",
    )


def _quota_fields(settings: Settings) -> html.Markup:
    quotas = settings.values.get("quota_by_level") or {}
    fields = []
    for level in range(models.MIN_LEVEL, models.MAX_LEVEL + 1):
        value = quotas.get(str(level), 0)
        fields.append(
            html.div(
                html.label(t("admin.settings.quota_level", level=level), for_=f"quota-{level}"),
                html.input_(
                    type="number",
                    name=f"quota_{level}",
                    id=f"quota-{level}",
                    value=str(value if value is not None else 0),
                    min="0",
                ),
                class_="field",
            )
        )
    return html.div(
        html.h2(t("admin.settings.quota_title")),
        html.p(t("admin.settings.quota_help"), class_="muted"),
        html.div(*fields, class_="grid-2"),
        class_="panel",
    )


def _settings_show(request: Request, *, error_notice: html.Markup | None = None) -> Response:
    settings = request.db.run_in_transaction(Settings.load)
    fields = [_setting_field(settings, key) for key in _SETTINGS_ORDER]
    form = html.form(
        _csrf(request),
        html.div(html.h2(t("admin.settings.title")), *fields, class_="panel"),
        _quota_fields(settings),
        _title_preset_field(settings),
        html.button(t("admin.settings.save"), type="submit"),
        method="post",
        action="/admin/settings",
    )
    banner = error_notice or _flash(request)
    return Response.html(
        page(
            request,
            t("admin.settings.title"),
            _admin_nav(request),
            form,
            banners=[banner] if banner else None,
        )
    )


def _settings_save(request: Request) -> Response:
    actor = _guard(request)
    form = request.form

    changes: dict[str, Any] = {}
    for key in _SETTINGS_ORDER:
        if key == "reminders_enabled":
            changes[key] = "on" if form.get(key) == "on" else "off"
        else:
            changes[key] = form.get(key, "")
    changes["quota_by_level"] = {
        str(level): form.get(f"quota_{level}", "0")
        for level in range(models.MIN_LEVEL, models.MAX_LEVEL + 1)
    }
    changes["title_presets"] = form.get("title_presets", "")

    def work(conn):
        before = Settings.load(conn)
        for key, raw in changes.items():
            new_value = settings_update(conn, key, raw, actor.id)
            if before.values.get(key) != new_value:
                audit.record(
                    conn,
                    actor_id=actor.id,
                    action=audit.SETTING_CHANGED,
                    target_type="setting",
                    target_id=key,
                    detail={"from": before.values.get(key), "to": new_value},
                )

    try:
        request.db.run_in_transaction(work)
    except AppError as exc:
        details = exc.details or {}
        key = details.get("key")
        reason = details.get("reason", "")
        if key:
            label = t(f"admin.settings.{key}.label") if key in SETTINGS_DEFAULTS else key
            reason_text = t(f"admin.settings.error.{reason}", **details)
            message = f"{label}: {reason_text}"
        else:
            message = error_message(exc.code)
        return _settings_show(request, error_notice=html.notice(message, kind="error"))

    return Response.redirect("/admin/settings?msg=settings_saved")


def _settings_get(request: Request) -> Response:
    _guard(request)
    return _settings_show(request)


# =============================================================================
# Email log
# =============================================================================


def _emails_list(request: Request) -> Response:
    _guard(request)
    rows = request.db.run_in_transaction(
        lambda conn: conn.query_all(
            "SELECT * FROM email_log ORDER BY created_at DESC LIMIT 500"
        )
    )
    if not rows:
        table = html.p(t("admin.emails.empty"), class_="muted")
    else:
        table_rows = []
        for row in rows:
            if mailer.can_resend(row):
                action = html.form(
                    _csrf(request),
                    html.button(t("admin.emails.resend"), type="submit",
                                class_="secondary"),
                    method="post",
                    action=f"/admin/emails/{row['id']}/resend",
                    class_="inline-form",
                )
            else:
                # Says why the button is missing. Without this the page just
                # looks inconsistent -- some rows have it, some do not.
                action = html.span(
                    t("admin.emails.resend.unavailable"), class_="muted"
                )
            table_rows.append(
                html.tr(
                    html.td(_fmt(row["created_at"])),
                    html.td(row["type"]),
                    html.td(row["to_email"]),
                    html.td(t(f"admin.emails.status_{row['status']}")),
                    html.td(row["error"] or "-"),
                    html.td(str(row["attempts"])),
                    html.td(action),
                )
            )
        table = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.emails.col_created_at")),
                        html.th(t("admin.emails.col_type")),
                        html.th(t("admin.emails.col_to")),
                        html.th(t("admin.emails.col_status")),
                        html.th(t("admin.emails.col_error")),
                        html.th(t("admin.emails.col_attempts")),
                        html.th(t("admin.emails.col_actions")),
                    )
                ),
                html.tbody(*table_rows),
            ),
            class_="table-wrap",
        )
    return _shell(request, t("admin.emails.title"), html.div(table, class_="panel"))


def _emails_resend(request: Request) -> Response:
    _guard(request)
    try:
        mailer.resend(request.db, request.params["email_id"])
    except AppError as exc:
        return Response.redirect(f"/admin/emails?err={exc.code}")
    return Response.redirect("/admin/emails?msg=email_resent")

# =============================================================================
# Audit trail
# =============================================================================


def _audit_action_label(action: str) -> str:
    key = f"admin.audit.action.{action}"
    label = t(key)
    return label if label != key else action


def _audit_list(request: Request) -> Response:
    _guard(request)
    rows = request.db.run_in_transaction(lambda conn: audit.recent(conn, limit=1000))

    if _wants_csv(request):
        import json as _json

        return _csv_response(
            "audit.csv",
            [
                t("admin.audit.csv_created_at"),
                t("admin.audit.csv_actor"),
                t("admin.audit.csv_action"),
                t("admin.audit.csv_target_type"),
                t("admin.audit.csv_target_id"),
                t("admin.audit.csv_detail"),
            ],
            (
                [
                    _fmt(row["created_at"]),
                    row["actor_name"] or "-",
                    _audit_action_label(row["action"]),
                    row["target_type"] or "",
                    row["target_id"] or "",
                    _json.dumps(row["detail"], ensure_ascii=False) if row["detail"] else "",
                ]
                for row in rows
            ),
        )

    if not rows:
        table = html.p(t("admin.audit.empty"), class_="muted")
    else:
        table_rows = []
        for row in rows:
            target = (
                f"{row['target_type']}:{row['target_id']}"
                if row["target_type"]
                else "-"
            )
            detail = ", ".join(f"{k}={v}" for k, v in (row["detail"] or {}).items())
            table_rows.append(
                html.tr(
                    html.td(_fmt(row["created_at"])),
                    html.td(row["actor_name"] or "-"),
                    html.td(_audit_action_label(row["action"])),
                    html.td(target),
                    html.td(detail or "-"),
                )
            )
        table = html.div(
            html.table(
                html.thead(
                    html.tr(
                        html.th(t("admin.audit.col_created_at")),
                        html.th(t("admin.audit.col_actor")),
                        html.th(t("admin.audit.col_action")),
                        html.th(t("admin.audit.col_target")),
                        html.th(t("admin.audit.col_detail")),
                    )
                ),
                html.tbody(*table_rows),
            ),
            class_="table-wrap",
        )

    export_link = html.p(
        html.a(t("common.export_csv"), href="/admin/audit?format=csv", class_="btn secondary")
    )
    return _shell(request, t("admin.audit.title"), export_link, html.div(table, class_="panel"))


# =============================================================================
# Registration
# =============================================================================


def register(router: Router) -> None:
    router.add("GET", "/admin", _dashboard)

    router.add("GET", "/admin/approvals", _approvals_list)
    router.add("POST", "/admin/approvals/{user_id}/approve", _approvals_approve)
    router.add("POST", "/admin/approvals/{user_id}/reject", _approvals_reject)

    router.add("GET", "/admin/members", _members_list)
    router.add("POST", "/admin/members/{user_id}/level", _members_set_level)
    router.add("GET", "/admin/members/{user_id}/delete", _members_delete_confirm)
    router.add("POST", "/admin/members/{user_id}/delete", _members_delete)
    router.add("POST", "/admin/members/{user_id}/suspend", _members_suspend)
    router.add("POST", "/admin/members/{user_id}/reactivate", _members_reactivate)

    router.add("GET", "/admin/invitations", _invitations_list)
    router.add("POST", "/admin/invitations", _invitations_send)
    router.add("POST", "/admin/invitations/{token_id}/revoke", _invitations_revoke)

    router.add("GET", "/admin/rooms", _rooms_list)
    router.add("POST", "/admin/rooms", _rooms_create)
    router.add("POST", "/admin/rooms/{room_id}", _rooms_update)
    router.add("POST", "/admin/rooms/{room_id}/deactivate", _rooms_deactivate)
    router.add("POST", "/admin/rooms/{room_id}/activate", _rooms_activate)

    router.add("GET", "/admin/bookings", _bookings_list)
    router.add("POST", "/admin/bookings/{booking_id}/cancel", _bookings_cancel)

    router.add("GET", "/admin/preemptions", _preemptions_list)

    router.add("GET", "/admin/settings", _settings_get)
    router.add("POST", "/admin/settings", _settings_save)

    router.add("GET", "/admin/emails", _emails_list)
    router.add("POST", "/admin/emails/{email_id}/resend", _emails_resend)

    router.add("GET", "/admin/audit", _audit_list)
