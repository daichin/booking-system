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
from urllib.parse import urlencode

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
    format_date,
    format_range,
    format_hhmm,
    local_date,
    minutes_since_midnight,
    now_utc,
    parse_hhmm,
    parse_utc,
)
from app.web.framework import (
    CSRF_FIELD,
    csrf_token,
    Request,
    Response,
    Router,
    require_login,
)
from app.web.html import (
    Markup,
    a,
    button,
    details,
    div,
    field,
    form,
    h2,
    h3,
    hidden,
    input_,
    label,
    li,
    notice,
    option,
    p,
    select,
    span,
    summary,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
    ul,
)
from app.web.layout import page


def format_range_current(start, end):
    """A booking window in the language of the request being handled."""
    from app.i18n import current_locale

    return format_range(start, end, current_locale())

_PENDING_STATUSES = (models.PENDING_EMAIL, models.PENDING_APPROVAL)

_TAG_CLASS = {
    models.CONFIRMED: "tag-confirmed",
    models.CANCELLED_BY_USER: "tag-cancelled",
    models.CANCELLED_BY_ADMIN: "tag-cancelled",
    models.PREEMPTED: "tag-preempted",
}


# --- small shared helpers ------------------------------------------------------


def _csrf_hidden(request: Request) -> Markup:
    return hidden(CSRF_FIELD, csrf_token(request))


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


def _read_title(data: dict) -> str:
    """The meeting title, from whichever control the member used.

    Each preset is its own submit button carrying ``name="title"``, so one
    click both chooses the title and submits the booking. The free-text box
    is named separately and wins when it has been filled in.
    """
    custom = str(data.get("custom_title", "")).strip()
    if custom:
        return custom
    return str(data.get("title", "")).strip()


def _recent_titles(db: Any, user_id: str, limit: int = 4) -> list[str]:
    """Titles this member has used before, most recent first.

    Cheaper than it looks and worth a lot: most people book the same few
    meetings over and over.
    """

    def work(conn: Any) -> list[str]:
        rows = conn.query_all(
            "SELECT title, MAX(start_at) AS latest FROM bookings"
            " WHERE user_id = ? GROUP BY title ORDER BY latest DESC LIMIT ?",
            (user_id, limit),
        )
        return [row["title"] for row in rows if row["title"]]

    return db.run_in_transaction(work)


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

    # Parse each field on its own. Wrapping all three in one try meant any
    # failure was reported as a missing "date", which sent people looking at
    # the one field that was actually fine.
    try:
        day = date.fromisoformat(str(data.get("date", "")))
    except (ValueError, TypeError):
        raise AppError(MISSING_FIELD, {"field": "date"})

    try:
        start_minutes = parse_hhmm(str(data.get("start_time", "")))
    except (ValueError, TypeError):
        raise AppError(MISSING_FIELD, {"field": "start_time"})

    try:
        end_minutes = parse_hhmm(str(data.get("end_time", "")))
    except (ValueError, TypeError):
        raise AppError(MISSING_FIELD, {"field": "end_time"})

    return combine_taipei(day, start_minutes), combine_taipei(day, end_minutes)


# --- day / week rendering --------------------------------------------------------


def _legend() -> Markup:
    return div(
        span(t("day.legend.mine"), class_="k-mine"),
        span(t("day.legend.other"), class_="k-other"),
        span(t("day.legend.free"), class_="k-free"),
        class_="legend",
    )


def _format_duration(minutes: int) -> str:
    """"90" -> "1.5 小時". Shown on every end-time option so nobody has to
    work out how long 09:30 to 11:00 is."""
    if minutes < 60:
        return t("day.duration_minutes", minutes=minutes)
    hours = minutes / 60
    return t("day.duration_hours", hours=f"{hours:g}")


def _booking_at(room_day: Any, minute: int) -> dict | None:
    """The confirmed booking covering this slot, if any."""
    for entry in room_day.bookings:
        start = minutes_since_midnight(entry["start_at"])
        end = minutes_since_midnight(entry["end_at"]) or 24 * 60
        if start <= minute < end:
            return entry
    return None


def _render_slots(
    request: Request,
    room_day: Any,
    viewer: models.User,
    day: date,
    settings: Settings,
    selection: tuple[str, int] | None,
) -> Markup:
    """One room's slots, as links that build a booking two clicks at a time.

    Click one: pick a start. Click two: pick where it ends. Between those the
    page is in a visibly different state, which is what tells the two clicks
    apart without any JavaScript.

    Occupied slots stay clickable: the preemption engine decides whether the
    member may take them, and says so on the confirmation page. The grid does
    not try to know the rules.
    """
    slot = settings.slot_minutes
    selecting_here = selection is not None and selection[0] == room_day.room.id
    start_minute = selection[1] if selecting_here else None

    items: list[Markup] = []
    minute = room_day.open_minutes
    while minute < room_day.close_minutes:
        slot_end = minute + slot
        booked = _booking_at(room_day, minute)
        time_label = f"{format_hhmm(minute)}–{format_hhmm(slot_end)}"

        classes = ["slot"]
        if booked is not None:
            classes.append("is-mine" if booked["user_id"] == viewer.id else "is-booked")

        # Both cases use the same wrapper so every row is the same height and
        # the columns stay in step; the full text lives in the title attribute
        # because the visible text is clipped when it does not fit.
        if booked is not None:
            owner_name = booked["owner"]["full_name"]
            detail: Any = div(
                span(booked["title"], class_="slot-title"),
                span(owner_name, class_="slot-owner"),
                class_="slot-detail",
                title=f"{booked['title']} ・ {owner_name}",
            )
        else:
            detail = div(
                span(t("day.slot_free"), class_="slot-free"), class_="slot-detail"
            )

        action: Any = Markup("")
        if viewer.can_book:
            if not selecting_here:
                action = a(
                    t("day.pick_start"),
                    href=_day_url(day, room_day.room.id, minute),
                    class_="slot-action",
                )
            elif minute == start_minute:
                classes.append("is-start")
                action = a(
                    t("day.only_this", duration=_format_duration(slot)),
                    href=_book_url(room_day.room.id, day, start_minute, slot_end),
                    class_="slot-action",
                )
            elif minute > start_minute:
                span_minutes = slot_end - start_minute
                if span_minutes <= settings.max_booking_minutes:
                    classes.append("is-in-range")
                    action = a(
                        t("day.end_here", duration=_format_duration(span_minutes)),
                        href=_book_url(room_day.room.id, day, start_minute, slot_end),
                        class_="slot-action",
                    )
                else:
                    classes.append("is-unreachable")
            else:
                classes.append("is-unreachable")

        items.append(
            li(
                span(time_label, class_="slot-time"),
                detail,
                action,
                class_=" ".join(classes),
                id=_slot_anchor(room_day.room.id, minute),
            )
        )
        minute = slot_end
    return ul(*items, class_="slot-list")


def _slot_anchor(room_id: str, minute: int) -> str:
    return f"slot-{room_id}-{minute}"


def _day_url(
    day: date,
    room_id: str | None = None,
    start_minute: int | None = None,
    *,
    anchor: str | None = None,
) -> str:
    """A link back to the day grid, landing where the member was looking.

    Picking a start reloads the page, and without a fragment the browser
    starts at the top -- so choosing an 18:00 slot threw you back to 08:00 and
    you had to scroll down again to pick the end. The fragment puts you back
    on the exact row you clicked.
    """
    query = {"date": day.isoformat()}
    if room_id and start_minute is not None:
        query["room"] = room_id
        query["start"] = format_hhmm(start_minute)
        anchor = anchor or _slot_anchor(room_id, start_minute)
    fragment = f"#{anchor}" if anchor else ""
    return f"/day?{urlencode(query)}{fragment}"


def _book_url(room_id: str, day: date, start_minute: int, end_minute: int) -> str:
    return "/book?" + urlencode(
        {
            "room_id": room_id,
            "date": day.isoformat(),
            "start_time": format_hhmm(start_minute),
            "end_time": format_hhmm(end_minute),
        }
    )


def _room_column(
    request: Request,
    room_day: Any,
    viewer: models.User,
    day: date,
    settings: Settings,
    selection: tuple[str, int] | None,
    header: str | None = None,
    anchor: str | None = None,
) -> Markup:
    """One column of the grid.

    The day view heads each column with the room name; the week view shows
    one room across seven days, so it passes the date instead. ``anchor`` is
    the id the jump list scrolls to.
    """
    selected = selection is not None and selection[0] == room_day.room.id
    return div(
        h3(header or room_day.room.name),
        _render_slots(request, room_day, viewer, day, settings, selection),
        class_="room-column" + (" is-selecting" if selected else ""),
        id=anchor,
    )


def _pending_notice(user: models.User) -> Markup:
    if user.status in _PENDING_STATUSES:
        return notice(error_message(AWAITING_APPROVAL), kind="warning")
    return Markup("")


def _booking_panel(request: Request, room_days: list, day: date, settings: Settings) -> Markup:
    room_options = [option(rd.room.name, value=rd.room.id) for rd in room_days]
    # The value must be the same "HH:MM" form the submitted value is parsed
    # back from. Sending minutes-past-midnight here instead made every
    # booking fail, because the parser only understands "HH:MM".
    time_options = [
        option(format_hhmm(minute), value=format_hhmm(minute))
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


def _date_bar(day: date) -> Markup:
    """Previous / today / jump-to-date / next.

    The jump uses a native ``<input type="date">``, which opens the phone's
    own calendar and needs no JavaScript.
    """
    return div(
        a(t("day.prev"), href=_day_url(day - timedelta(days=1)), class_="btn secondary"),
        a(t("day.today"), href=_day_url(local_date(now_utc())), class_="btn secondary"),
        form(
            input_(type="date", name="date", value=day.isoformat(), aria_label=t("day.goto")),
            button(t("day.go"), type="submit", class_="secondary"),
            method="get",
            action="/day",
            class_="date-jump",
        ),
        a(t("day.next"), href=_day_url(day + timedelta(days=1)), class_="btn secondary"),
        class_="date-bar",
    )


def _jump_list(summary_text: str, entries: list[tuple[str, str, bool]]) -> Markup:
    """A dropdown that scrolls or navigates to one of ``entries``.

    ``<details>`` rather than a ``<select>``: a select needs JavaScript to act
    on a choice, and nothing else in this app does. Smooth scrolling comes
    from CSS, and is disabled for readers who ask for reduced motion.

    Each entry is ``(href, label, is_current)``.
    """
    return details(
        summary(summary_text),
        ul(
            *[
                li(a(label, href=href, aria_current="true" if current else None))
                for href, label, current in entries
            ],
            class_="jump-list",
        ),
        class_="jump",
    )


def _room_anchor(room_id: str) -> str:
    return f"room-{room_id}"


def _parse_selection(request: Request) -> tuple[str, int] | None:
    """``?room=&start=HH:MM`` -- the half-finished pick carried in the URL."""
    room_id = request.query.get("room")
    raw_start = request.query.get("start")
    if not room_id or not raw_start:
        return None
    try:
        return room_id, parse_hhmm(raw_start)
    except ValueError:
        return None


def _selection_banner(
    day: date, room_id: str, room_name: str, start_minute: int
) -> Markup:
    return div(
        span(
            t(
                "day.selected",
                room=room_name,
                time=format_hhmm(start_minute),
            ),
            class_="selection-text",
        ),
        # Cancelling keeps your place too, or you are thrown back to the top
        # of the page for choosing wrongly.
        a(
            t("day.cancel_selection"),
            href=_day_url(day, anchor=_slot_anchor(room_id, start_minute)),
            class_="btn secondary",
        ),
        class_="selection-banner",
        role="status",
    )


def day_view(request: Request) -> Response:
    user = require_login(request)
    day = _parse_view_date(request)
    settings = request.db.run_in_transaction(Settings.load)
    room_days = rooms.availability(request.db, day=day)
    selection = _parse_selection(request) if user.can_book else None

    parts: list[Any] = [_pending_notice(user)]
    parts.append(_date_bar(day))
    parts.append(p(format_date(combine_taipei(day, 0), request.locale), class_="day-heading"))

    if selection is not None:
        chosen = next(
            (rd for rd in room_days if rd.room.id == selection[0]), None
        )
        if chosen is None:
            selection = None
        else:
            parts.append(
                _selection_banner(day, chosen.room.id, chosen.room.name, selection[1])
            )

    if not selection and user.can_book:
        parts.append(p(t("day.hint_pick_start"), class_="muted"))

    parts.append(_legend())

    if not room_days:
        parts.append(p(t("day.no_rooms"), class_="muted"))
    else:
        # Worth the row only when there is somewhere to jump to.
        if len(room_days) > 1:
            parts.append(
                _jump_list(
                    t("day.jump_to_room"),
                    [
                        (
                            f"#{_room_anchor(rd.room.id)}",
                            rd.room.name,
                            selection is not None and selection[0] == rd.room.id,
                        )
                        for rd in room_days
                    ],
                )
            )
        columns = [
            _room_column(
                request,
                room_day,
                user,
                day,
                settings,
                selection,
                anchor=_room_anchor(room_day.room.id),
            )
            for room_day in room_days
        ]
        parts.append(div(*columns, class_="day-grid cols-3"))
        if user.can_book:
            parts.append(
                details(
                    summary(t("day.manual_entry")),
                    _booking_panel(request, room_days, day, settings),
                    class_="manual-entry",
                )
            )

    return Response.html(page(request, t("nav.day"), *parts))


def _week_jump(room_id: str, showing: date, settings: Settings) -> Markup:
    """Jump straight to any week, instead of stepping one at a time.

    The range covers the whole bookable horizon plus the week just gone, so
    every week a member can actually book is one tap away. Labels are numeric
    date ranges, which read the same in both languages.
    """
    today = local_date(now_utc())
    first = today - timedelta(days=today.weekday() + 7)   # last Monday but one
    last = today + timedelta(days=settings.booking_horizon_days)

    entries: list[tuple[str, str, bool]] = []
    monday = first
    while monday <= last:
        end = monday + timedelta(days=6)
        label = f"{monday:%m/%d} – {end:%m/%d}"
        if monday <= today <= end:
            label = f"{label} ・ {t('week.this_week')}"
        entries.append(
            (
                f"/week?{urlencode({'room_id': room_id, 'date': monday.isoformat()})}",
                label,
                monday <= showing <= end,
            )
        )
        monday += timedelta(days=7)

    return _jump_list(t("week.jump_to_week"), entries)


def book_view(request: Request) -> Response:
    """Confirm a time picked on the day grid, and name the meeting.

    Room, date and both times arrive in the URL from the two clicks the
    member already made, so all that is left is the title -- and each preset
    is a submit button, so choosing one finishes the booking in a single
    click.
    """
    user = require_login(request)
    _require_can_book(user)

    room_id = request.query.get("room_id", "")
    day = _parse_view_date(request)
    start_at, end_at = _parse_times(request.query)
    settings = request.db.run_in_transaction(Settings.load)

    room = request.db.run_in_transaction(lambda conn: rooms.get_room(conn, room_id))

    # Probe with a placeholder title: the real one is chosen below and
    # validated on submit. This only asks "is this slot obtainable?".
    probe = preemption.attempt_booking(
        request.db,
        requester_id=user.id,
        room_id=room_id,
        start_at=start_at,
        end_at=end_at,
        title=t("book.placeholder_title"),
        dry_run=True,
    )

    duration = _format_duration(int((end_at - start_at).total_seconds() // 60))
    summary_line = div(
        p(
            span(room.name, class_="slot-title"),
            span(" ・ ", class_="muted"),
            span(format_range(start_at, end_at, request.locale)),
            span(f" ({duration})", class_="muted"),
        ),
        p(a(t("book.change_time"), href=_day_url(day, room_id, minutes_since_midnight(start_at)))),
        class_="panel",
    )

    parts: list[Any] = [summary_line]

    if probe.outcome == BLOCKED:
        parts.append(notice(error_message(probe.reason or "", **(probe.blocker or {})), kind="error"))
        parts.append(p(a(t("book.back_to_day"), href=_day_url(day), class_="btn")))
        return Response.html(page(request, t("book.title"), *parts))

    confirm_override = probe.outcome == PREEMPTION_REQUIRED
    if confirm_override:
        parts.append(
            div(
                h2(t("book.will_override_title")),
                p(t("book.will_override", count=len(probe.victims))),
                ul(
                    *[
                        li(
                            f"{v.room_name} ・ "
                            f"{format_range(v.booking.start_at, v.booking.end_at, request.locale)}"
                            f" ・ {v.owner_view['full_name']}"
                            f"（{v.owner_view['department']}）"
                        )
                        for v in probe.victims
                    ],
                    class_="victim-list",
                ),
                class_="confirm-panel",
            )
        )

    presets = settings.title_presets
    recent = [title for title in _recent_titles(request.db, user.id) if title not in presets]

    hidden_fields = [
        _csrf_hidden(request),
        hidden("room_id", room_id),
        hidden("date", day.isoformat()),
        hidden("start_time", format_hhmm(minutes_since_midnight(start_at))),
        hidden("end_time", format_hhmm(minutes_since_midnight(end_at))),
    ]
    if confirm_override:
        hidden_fields.append(hidden("confirm_preemption", "1"))

    submit_label = (
        t("book.submit_override") if confirm_override else t("book.submit")
    )

    chip_rows: list[Any] = []
    if presets:
        chip_rows.append(p(t("book.pick_subject"), class_="muted"))
        chip_rows.append(
            div(
                *[
                    button(name, type="submit", name_="title", value=name, class_="chip")
                    for name in presets
                ],
                class_="chips",
            )
        )
    if recent:
        chip_rows.append(p(t("book.recent"), class_="muted"))
        chip_rows.append(
            div(
                *[
                    button(name, type="submit", name_="title", value=name, class_="chip secondary")
                    for name in recent
                ],
                class_="chips",
            )
        )

    parts.append(
        div(
            h2(t("book.subject")),
            form(
                *hidden_fields,
                *chip_rows,
                div(
                    label(t("book.custom"), for_="f-custom-title"),
                    input_(
                        type="text",
                        name="custom_title",
                        id="f-custom-title",
                        maxlength="120",
                        placeholder=t("book.custom_placeholder"),
                    ),
                    class_="field",
                ),
                div(button(submit_label, type="submit"), class_="actions"),
                method="post",
                action="/bookings",
            ),
            class_="panel",
        )
    )

    return Response.html(page(request, t("book.title"), *parts))


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

    parts.append(_week_jump(selected_room.id, start_day, settings))

    columns = []
    for offset in range(7):
        day = start_day + timedelta(days=offset)
        [room_day] = rooms.availability(request.db, day=day, room_ids=[selected_room.id])
        # Picking a slot here jumps to that day's grid with the start already
        # chosen, so the week view is a way in to the same two-click flow.
        columns.append(
            _room_column(
                request,
                room_day,
                user,
                day,
                settings,
                None,
                header=format_date(combine_taipei(day, 0), request.locale),
            )
        )
    # Four across, so seven days fold into two rows rather than squeezing
    # each day too narrow to read a booking title.
    parts.append(div(*columns, class_="day-grid cols-4"))

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
                td(format_range_current(row["start_at"], row["end_at"])),
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
                td(format_range_current(row["start_at"], row["end_at"])),
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
            f"{victim.room_name}｜{format_range_current(victim.booking.start_at, victim.booking.end_at)}"
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
            detail += "｜" + format_range_current(parse_utc(blocker["start_at"]), parse_utc(blocker["end_at"]))
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
    title = _read_title(form_data)
    start_at, end_at = _parse_times(form_data)
    confirm = form_data.get("confirm_preemption") in ("true", "1")

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
    router.add("GET", "/book", book_view)
    router.add("GET", "/week", week_view)
    router.add("GET", "/my", my_bookings)
    router.add("POST", "/bookings", bookings_form)
    router.add("POST", "/bookings/{id}/cancel", cancel_booking_form)
    router.add("POST", "/api/bookings/check", api_check_booking)
    router.add("POST", "/api/bookings", api_create_booking)
    router.add("POST", "/api/bookings/{id}/cancel", api_cancel_booking)
    router.add("GET", "/api/availability", api_availability)
