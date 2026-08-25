"""Member screens and the booking JSON API (spec §8, §7.2): Task 5.

Day view, week view, "my bookings", and booking creation/cancellation. The
two-phase preemption UX (spec §7.2) is the centrepiece: phase 1 is a dry run
that reports ``AVAILABLE`` / ``PREEMPTION_REQUIRED`` / ``BLOCKED``; phase 2
only commits on explicit confirmation and always re-runs the whole check
inside :func:`app.services.preemption.attempt_booking`'s own transaction --
this module never trusts a phase-1 result.

``POST /bookings`` is the no-JavaScript path: the same two phases, but the
confirmation step is a server-rendered page with a form instead of a client
dialog, so the system stays usable with JavaScript disabled. The JSON
endpoints under ``/api/bookings`` exist for the same two phases when a
client wants to script them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app import models
from app.errors import (
    AVAILABLE,
    AWAITING_APPROVAL,
    BLOCKED,
    CREATED,
    MISSING_FIELD,
    NOT_ACTIVE,
    PREEMPTION_REQUIRED,
    AppError,
    ForbiddenError,
)
from app.i18n import error_message, t
from app.services import bookings, mailer, preemption, rooms
from app.settings import Settings
from app.timeutil import (
    combine_taipei,
    format_date_zh,
    format_hhmm,
    format_range_zh,
    local_date,
    minutes_since_midnight,
    now_utc,
    parse_hhmm,
    parse_utc,
)
from app.web.framework import (
    CSRF_COOKIE,
    CSRF_FIELD,
    Request,
    Response,
    Router,
    require_login,
)
from app.web.html import (
    Markup,
    a,
    button,
    div,
    field,
    form,
    h2,
    h3,
    hidden,
    label,
    li,
    notice,
    option,
    p,
    select,
    span,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
    ul,
)
from app.web.layout import page

_PENDING_STATUSES = (models.PENDING_EMAIL, models.PENDING_APPROVAL)

_TAG_CLASS = {
    models.CONFIRMED: "tag-confirmed",
    models.CANCELLED_BY_USER: "tag-cancelled",
    models.CANCELLED_BY_ADMIN: "tag-cancelled",
    models.PREEMPTED: "tag-preempted",
}


# --- small shared helpers ------------------------------------------------------


def _csrf_hidden(request: Request) -> Markup:
    return hidden(CSRF_FIELD, request.cookies.get(CSRF_COOKIE, ""))


def _parse_view_date(request: Request, param: str = "date") -> date:
    raw = request.query.get(param, "")
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return local_date(now_utc())


def _require_can_book(user: models.User) -> None:
    if user.status in _PENDING_STATUSES:
        raise ForbiddenError(AWAITING_APPROVAL)
    if not user.can_book:
        raise ForbiddenError(NOT_ACTIVE, {"status": user.status})


def _select_field(name: str, caption: str, options: list) -> Markup:
    return div(
        label(caption, for_=f"f-{name}"),
        select(*options, name=name, id=f"f-{name}", required=True),
        class_="field",
    )


def _parse_times(data: dict) -> tuple[datetime, datetime]:
    """Build ``(start_at, end_at)`` from either an ISO-pair or a date+slot form.

    JSON callers (and the phase-2 confirmation form) send ``start_at`` /
    ``end_at`` directly as ISO-8601 strings. The initial quick-book form sends
    a Taipei calendar date plus two ``HH:MM`` slot boundaries instead, because
    that is how a member picks a time with no JavaScript.
    """
    if data.get("start_at") and data.get("end_at"):
        try:
            return parse_utc(str(data["start_at"])), parse_utc(str(data["end_at"]))
        except ValueError:
            raise AppError(MISSING_FIELD, {"field": "start_at"})

    try:
        day = date.fromisoformat(str(data.get("date", "")))
        start_minutes = parse_hhmm(str(data.get("start_time", "")))
        end_minutes = parse_hhmm(str(data.get("end_time", "")))
    except (ValueError, TypeError):
        raise AppError(MISSING_FIELD, {"field": "date"})
    return combine_taipei(day, start_minutes), combine_taipei(day, end_minutes)


# --- day / week rendering --------------------------------------------------------


def _legend() -> Markup:
    return div(
        span(t("day.legend.mine"), class_="k-mine"),
        span(t("day.legend.other"), class_="k-other"),
        span(t("day.legend.free"), class_="k-free"),
        class_="legend",
    )


def _render_slots(room_day: Any, viewer_id: str, slot_minutes: int) -> Markup:
    items = []
    minute = room_day.open_minutes
    while minute < room_day.close_minutes:
        slot_end = minute + slot_minutes
        booked = None
        for entry in room_day.bookings:
            b_start = minutes_since_midnight(entry["start_at"])
            b_end = minutes_since_midnight(entry["end_at"]) or 24 * 60
            if b_start <= minute < b_end:
                booked = entry
                break

        time_label = f"{format_hhmm(minute)}–{format_hhmm(slot_end)}"
        if booked is None:
            items.append(
                li(
                    span(time_label, class_="slot-time"),
                    span(t("day.slot_free"), class_="slot-free"),
                    class_="slot",
                )
            )
        else:
            is_mine = booked["user_id"] == viewer_id
            css = "slot is-booked" + (" is-mine" if is_mine else "")
            items.append(
                li(
                    span(time_label, class_="slot-time"),
                    div(
                        span(booked["title"], class_="slot-title"),
                        span(booked["owner"]["full_name"], class_="slot-owner"),
                    ),
                    class_=css,
                )
            )
        minute = slot_end
    return ul(*items, class_="slot-list")


def _room_column(header: str, room_day: Any, viewer_id: str, slot_minutes: int) -> Markup:
    return div(h3(header), _render_slots(room_day, viewer_id, slot_minutes), class_="room-column")


def _pending_notice(user: models.User) -> Markup:
    if user.status in _PENDING_STATUSES:
        return notice(error_message(AWAITING_APPROVAL), kind="warning")
    return Markup("")


def _booking_panel(request: Request, room_days: list, day: date, settings: Settings) -> Markup:
    room_options = [option(rd.room.name, value=rd.room.id) for rd in room_days]
    time_options = [
        option(format_hhmm(minute), value=str(minute))
        for minute in range(0, 24 * 60 + 1, settings.slot_minutes)
    ]
    return div(
        h2(t("day.book.title")),
        form(
            _csrf_hidden(request),
            _select_field("room_id", t("day.book.room"), room_options),
            field("date", t("day.book.date"), type="date", value=day.isoformat()),
            _select_field("start_time", t("day.book.start"), time_options),
            _select_field("end_time", t("day.book.end"), time_options),
            field("title", t("day.book.subject")),
            div(button(t("day.book.submit"), type="submit"), class_="actions"),
            method="post",
            action="/bookings",
        ),
        class_="panel",
    )


def day_view(request: Request) -> Response:
    user = require_login(request)
    day = _parse_view_date(request)
    settings = request.db.run_in_transaction(Settings.load)
    room_days = rooms.availability(request.db, day=day)

    parts: list[Any] = [_pending_notice(user)]
    parts.append(
        div(
            a(t("day.prev"), href=f"/day?date={day - timedelta(days=1)}"),
            span(format_date_zh(combine_taipei(day, 0))),
            a(t("day.next"), href=f"/day?date={day + timedelta(days=1)}"),
            class_="actions",
        )
    )
    parts.append(_legend())

    if not room_days:
        parts.append(p(t("day.no_rooms"), class_="muted"))
    else:
        columns = [
            _room_column(room_day.room.name, room_day, user.id, settings.slot_minutes)
            for room_day in room_days
        ]
        parts.append(div(*columns, class_="day-grid"))
        if user.can_book:
            parts.append(_booking_panel(request, room_days, day, settings))

    return Response.html(page(request, t("nav.day"), *parts))


def week_view(request: Request) -> Response:
    user = require_login(request)
    active_rooms = rooms.list_rooms(request.db)
    if not active_rooms:
        return Response.html(page(request, t("nav.week"), p(t("day.no_rooms"), class_="muted")))

    requested_room_id = request.query.get("room_id") or active_rooms[0].id
    selected_room = next((r for r in active_rooms if r.id == requested_room_id), active_rooms[0])
    start_day = _parse_view_date(request)
    settings = request.db.run_in_transaction(Settings.load)

    parts: list[Any] = [_pending_notice(user)]

    room_options = [
        option(r.name, value=r.id, selected=True if r.id == selected_room.id else None)
        for r in active_rooms
    ]
    parts.append(
        form(
            _select_field("room_id", t("week.room"), room_options),
            hidden("date", start_day.isoformat()),
            div(button(t("week.view"), type="submit"), class_="actions"),
            method="get",
            action="/week",
        )
    )
    parts.append(
        div(
            a(t("week.prev"), href=f"/week?room_id={selected_room.id}&date={start_day - timedelta(days=7)}"),
            a(t("week.next"), href=f"/week?room_id={selected_room.id}&date={start_day + timedelta(days=7)}"),
            class_="actions",
        )
    )
    parts.append(_legend())

    columns = []
    for offset in range(7):
        day = start_day + timedelta(days=offset)
        [room_day] = rooms.availability(request.db, day=day, room_ids=[selected_room.id])
        columns.append(
            _room_column(format_date_zh(combine_taipei(day, 0)), room_day, user.id, settings.slot_minutes)
        )
    parts.append(div(*columns, class_="day-grid"))

    return Response.html(page(request, t("nav.week"), *parts))


# --- my bookings ----------------------------------------------------------------


def _upcoming_table(request: Request, rows: list[dict]) -> Markup:
    body_rows = []
    for row in rows:
        cancel_form = form(
            _csrf_hidden(request),
            button(t("my.cancel"), type="submit", class_="danger"),
            method="post",
            action=f"/bookings/{row['id']}/cancel",
            class_="inline-form",
        )
        body_rows.append(
            tr(
                td(row["room_name"]),
                td(row["title"]),
                td(format_range_zh(row["start_at"], row["end_at"])),
                td(cancel_form),
            )
        )
    return div(
        table(
            thead(tr(th(t("my.room")), th(t("my.title_col")), th(t("my.time")), th(""))),
            tbody(*body_rows),
        ),
        class_="table-wrap",
    )


def _past_table(rows: list[dict]) -> Markup:
    body_rows = []
    for row in rows:
        status_label = t(f"booking_status.{row['status']}")
        tag = span(status_label, class_=f"tag {_TAG_CLASS.get(row['status'], '')}")
        body_rows.append(
            tr(
                td(row["room_name"]),
                td(row["title"]),
                td(format_range_zh(row["start_at"], row["end_at"])),
                td(tag),
            )
        )
    return div(
        table(
            thead(tr(th(t("my.room")), th(t("my.title_col")), th(t("my.time")), th(t("my.status")))),
            tbody(*body_rows),
        ),
        class_="table-wrap",
    )


def my_bookings(request: Request) -> Response:
    user = require_login(request)
    upcoming, past = bookings.list_for_user(request.db, user.id)

    parts: list[Any] = []
    if request.query.get("booked") == "1":
        parts.append(notice(t("my.booked_success"), kind="success"))
    if request.query.get("cancelled") == "1":
        parts.append(notice(t("my.cancelled_success"), kind="success"))

    parts.append(h2(t("my.upcoming")))
    parts.append(_upcoming_table(request, upcoming) if upcoming else p(t("my.none_upcoming"), class_="muted"))

    parts.append(h2(t("my.past")))
    parts.append(_past_table(past) if past else p(t("my.none_past"), class_="muted"))

    return Response.html(page(request, t("nav.my_bookings"), *parts))


# --- booking creation: non-JS two-phase form (spec §7.2) -------------------------


def _confirmation_page(
    request: Request, room_id: str, start_at: datetime, end_at: datetime, title: str, victims: list
) -> Markup:
    items = [
        li(
            f"{victim.room_name}｜{format_range_zh(victim.booking.start_at, victim.booking.end_at)}"
            f"｜{victim.owner_view['full_name']}（{victim.owner_view['department']}）"
        )
        for victim in victims
    ]
    return div(
        p(t("booking.confirm.message", count=len(victims))),
        ul(*items, class_="victim-list"),
        form(
            _csrf_hidden(request),
            hidden("room_id", room_id),
            hidden("start_at", start_at.isoformat()),
            hidden("end_at", end_at.isoformat()),
            hidden("title", title),
            hidden("confirm_preemption", "true"),
            div(
                button(t("booking.confirm.submit"), type="submit", class_="danger"),
                a(t("booking.confirm.cancel"), href="/day", class_="btn secondary"),
                class_="actions",
            ),
            method="post",
            action="/bookings",
        ),
        class_="confirm-panel",
    )


def _blocked_page(attempt: Any) -> Markup:
    parts: list[Any] = [notice(error_message(attempt.reason), kind="error")]
    blocker = attempt.blocker or {}
    if blocker.get("room_name"):
        detail = blocker["room_name"]
        if blocker.get("start_at") and blocker.get("end_at"):
            detail += "｜" + format_range_zh(parse_utc(blocker["start_at"]), parse_utc(blocker["end_at"]))
        parts.append(p(detail, class_="muted"))
    owner = blocker.get("owner")
    if owner:
        parts.append(p(f"{owner['full_name']}（{owner['department']}）", class_="muted"))
    parts.append(p(a(t("booking.blocked.back"), href="/day")))
    return div(*parts, class_="panel stack")


def bookings_form(request: Request) -> Response:
    user = require_login(request)
    _require_can_book(user)

    form_data = request.form
    room_id = form_data.get("room_id", "")
    title = form_data.get("title", "")
    start_at, end_at = _parse_times(form_data)
    confirm = form_data.get("confirm_preemption") == "true"

    if confirm:
        attempt = preemption.attempt_booking(
            request.db,
            requester_id=user.id,
            room_id=room_id,
            start_at=start_at,
            end_at=end_at,
            title=title,
            confirm_preemption=True,
            dry_run=False,
        )
    else:
        attempt = preemption.attempt_booking(
            request.db,
            requester_id=user.id,
            room_id=room_id,
            start_at=start_at,
            end_at=end_at,
            title=title,
            dry_run=True,
        )
        if attempt.outcome == AVAILABLE:
            attempt = preemption.attempt_booking(
                request.db,
                requester_id=user.id,
                room_id=room_id,
                start_at=start_at,
                end_at=end_at,
                title=title,
                dry_run=False,
            )

    if attempt.outcome == CREATED:
        return Response.redirect("/my?booked=1")
    if attempt.outcome == PREEMPTION_REQUIRED:
        body = _confirmation_page(request, room_id, start_at, end_at, title, attempt.victims)
        return Response.html(page(request, t("booking.confirm.title"), body))
    if attempt.outcome == BLOCKED:
        body = _blocked_page(attempt)
        return Response.html(page(request, t("booking.blocked.title"), body), 409)
    raise AppError("INTERNAL")  # pragma: no cover - attempt_booking has no other outcome


def cancel_booking_form(request: Request) -> Response:
    user = require_login(request)
    result = bookings.cancel_booking(request.db, actor=user, booking_id=request.params["id"])
    mailer.enqueue(request.db, result.emails)
    return Response.redirect("/my?cancelled=1")


# --- JSON API (CONTRACT.md §6) ---------------------------------------------------


def api_check_booking(request: Request) -> Response:
    user = require_login(request)
    _require_can_book(user)
    body = request.json if isinstance(request.json, dict) else {}
    start_at, end_at = _parse_times(body)
    attempt = preemption.attempt_booking(
        request.db,
        requester_id=user.id,
        room_id=body.get("room_id", ""),
        start_at=start_at,
        end_at=end_at,
        title=body.get("title", ""),
        dry_run=True,
    )
    return Response.json(attempt.to_dict())


def api_create_booking(request: Request) -> Response:
    user = require_login(request)
    _require_can_book(user)
    body = request.json if isinstance(request.json, dict) else {}
    start_at, end_at = _parse_times(body)
    attempt = preemption.attempt_booking(
        request.db,
        requester_id=user.id,
        room_id=body.get("room_id", ""),
        start_at=start_at,
        end_at=end_at,
        title=body.get("title", ""),
        confirm_preemption=bool(body.get("confirm_preemption")),
        dry_run=False,
    )
    return Response.json(attempt.to_dict())


def api_cancel_booking(request: Request) -> Response:
    user = require_login(request)
    result = bookings.cancel_booking(request.db, actor=user, booking_id=request.params["id"])
    mailer.enqueue(request.db, result.emails)
    return Response.json({"ok": True})


def api_availability(request: Request) -> Response:
    require_login(request)
    day = _parse_view_date(request)
    room_id = request.query.get("room_id")
    room_ids = [room_id] if room_id else None
    room_days = rooms.availability(request.db, day=day, room_ids=room_ids)
    payload = {
        "rooms": [
            {
                "id": room_day.room.id,
                "name": room_day.room.name,
                "open": format_hhmm(room_day.open_minutes),
                "close": format_hhmm(room_day.close_minutes),
                "bookings": [
                    {
                        "id": entry["id"],
                        "title": entry["title"],
                        "start_at": entry["start_at"].isoformat(),
                        "end_at": entry["end_at"].isoformat(),
                        "owner": entry["owner"],
                    }
                    for entry in room_day.bookings
                ],
            }
            for room_day in room_days
        ]
    }
    return Response.json(payload)


# --- registration ------------------------------------------------------------


def register(router: Router) -> None:
    router.add("GET", "/day", day_view)
    router.add("GET", "/week", week_view)
    router.add("GET", "/my", my_bookings)
    router.add("POST", "/bookings", bookings_form)
    router.add("POST", "/bookings/{id}/cancel", cancel_booking_form)
    router.add("POST", "/api/bookings/check", api_check_booking)
    router.add("POST", "/api/bookings", api_create_booking)
    router.add("POST", "/api/bookings/{id}/cancel", api_cancel_booking)
    router.add("GET", "/api/availability", api_availability)
