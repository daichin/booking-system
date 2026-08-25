"""The E1-E10 email catalogue (spec §9.1) plus E1_EXISTS.

Every renderer takes a plain ``context`` dict (built by the caller -- Task 1
for account emails, Task 3/4 for booking emails, this module's own
:mod:`app.services.mailer` for reminders and the admin digest) and returns a
:class:`RenderedEmail` carrying a subject, an HTML part, and a text-only
fallback part. Rendering is pure -- it never touches the database or the
network -- so it can be called freely from tests.

All wording lives in ``app/i18n/zh_TW_email.py``; this module only assembles
it. Every email that shows a time uses :func:`app.timeutil.format_range_zh`,
which always carries the ``(台北時間)`` label (spec §9.2).

Expected ``context`` keys per kind
-----------------------------------
E1              full_name, verify_url, expires_hours (optional, default 24)
E1_EXISTS       login_url (optional, defaults to base_url)
E2              full_name, login_url (optional)
E3              full_name
E4              full_name, room_name, title, start_at, end_at,
                cancel_url (optional)
E5              full_name, room_name, title, start_at, end_at,
                reason ("preempted" | "cancelled_by_admin" | "room_deactivated"),
                book_url (optional).
                MUST NOT include anything about who preempted the victim --
                spec §7.1 / §12 C11. This module never reads such a field even
                if a caller mistakenly supplies one.
E6              full_name, room_name, title, start_at, end_at
E7              admin_name, pending (list of dicts with full_name, department,
                phone, email), admin_url (optional)
E8              invite_url, expires_hours (optional, default 168)
E9              full_name, reset_url, expires_hours (optional, default 2)
E10             full_name, room_name, title, start_at, end_at
"""

from __future__ import annotations

import html as html_lib
from dataclasses import dataclass
from typing import Any, Callable

from app.config import load_config
from app.i18n import t
from app.timeutil import format_range_zh

E5_PREEMPTED = "preempted"
E5_ADMIN = "cancelled_by_admin"
E5_ROOM = "room_deactivated"


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def _app_name() -> str:
    return t("app.name")


def _base_url() -> str:
    return load_config().base_url


def _link(context: dict[str, Any], key: str, default_path: str) -> str:
    """A context-supplied URL, or a generic fallback built from ``base_url``.

    Callers that already hold a token-bearing link (verification, invite,
    reset) always pass it explicitly; this fallback only covers plain
    navigation links (e.g. "go book another slot") that other tasks might
    not always populate.
    """
    url = context.get(key)
    if url:
        return str(url)
    return f"{_base_url()}{default_path}"


def _compose(subject: str, body: str) -> RenderedEmail:
    """Wrap a paragraph-separated body into HTML + text parts with a footer.

    All dynamic content is escaped for the HTML part -- booking titles and
    member names are user-supplied and must never be interpreted as markup.
    """
    footer = t("email.footer")
    text = f"{body}\n\n{footer}"

    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    html_paragraphs = "".join(
        f"<p>{html_lib.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
    )
    html_footer = (
        f'<p style="color:#888888;font-size:12px;margin-top:24px">'
        f"{html_lib.escape(footer)}</p>"
    )
    html_doc = (
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        f"<title>{html_lib.escape(subject)}</title></head>"
        f'<body style="font-family:sans-serif;line-height:1.6;color:#222222">'
        f"{html_paragraphs}{html_footer}</body></html>"
    )
    return RenderedEmail(subject=subject, html=html_doc, text=text)


def _e1(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E1.subject", app_name=app_name)
    body = t(
        "email.E1.body",
        app_name=app_name,
        full_name=context["full_name"],
        expires_hours=context.get("expires_hours", 24),
        verify_url=context["verify_url"],
    )
    return _compose(subject, body)


def _e1_exists(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E1_EXISTS.subject", app_name=app_name)
    body = t(
        "email.E1_EXISTS.body",
        login_url=_link(context, "login_url", "/login"),
    )
    return _compose(subject, body)


def _e2(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E2.subject", app_name=app_name)
    body = t(
        "email.E2.body",
        full_name=context["full_name"],
        login_url=_link(context, "login_url", "/login"),
    )
    return _compose(subject, body)


def _e3(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E3.subject", app_name=app_name)
    body = t("email.E3.body", full_name=context["full_name"])
    return _compose(subject, body)


def _e4(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E4.subject", app_name=app_name)
    body = t(
        "email.E4.body",
        full_name=context["full_name"],
        room_name=context["room_name"],
        title=context["title"],
        time_range=format_range_zh(context["start_at"], context["end_at"]),
        cancel_url=_link(context, "cancel_url", "/my"),
    )
    return _compose(subject, body)


_E5_KEYS = {
    E5_PREEMPTED: "email.E5_preempted",
    E5_ADMIN: "email.E5_admin",
    E5_ROOM: "email.E5_room",
}


def _e5(context: dict[str, Any]) -> RenderedEmail:
    reason = context.get("reason", E5_ADMIN)
    base_key = _E5_KEYS.get(reason, _E5_KEYS[E5_ADMIN])
    app_name = _app_name()
    subject = t(f"{base_key}.subject", app_name=app_name)
    # Deliberately built from an explicit allow-list of keys only -- spec
    # §7.1 / §12 C11: the preempting member's identity must never appear
    # here, so nothing from context beyond these fields is ever read.
    body = t(
        f"{base_key}.body",
        full_name=context["full_name"],
        room_name=context["room_name"],
        title=context["title"],
        time_range=format_range_zh(context["start_at"], context["end_at"]),
        book_url=_link(context, "book_url", "/day"),
    )
    return _compose(subject, body)


def _e6(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E6.subject", app_name=app_name)
    body = t(
        "email.E6.body",
        full_name=context["full_name"],
        room_name=context["room_name"],
        title=context["title"],
        time_range=format_range_zh(context["start_at"], context["end_at"]),
    )
    return _compose(subject, body)


def _e7(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    pending = context.get("pending", [])
    pending_list = "\n".join(
        t(
            "email.E7.pending_item",
            full_name=p["full_name"],
            department=p["department"],
            phone=p["phone"],
            email=p["email"],
        )
        for p in pending
    )
    subject = t("email.E7.subject", app_name=app_name)
    body = t(
        "email.E7.body",
        admin_name=context["admin_name"],
        count=len(pending),
        pending_list=pending_list,
        admin_url=_link(context, "admin_url", "/admin/approvals"),
    )
    return _compose(subject, body)


def _e8(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E8.subject", app_name=app_name)
    body = t(
        "email.E8.body",
        app_name=app_name,
        expires_hours=context.get("expires_hours", 168),
        invite_url=context["invite_url"],
    )
    return _compose(subject, body)


def _e9(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E9.subject", app_name=app_name)
    body = t(
        "email.E9.body",
        full_name=context["full_name"],
        expires_hours=context.get("expires_hours", 2),
        reset_url=context["reset_url"],
    )
    return _compose(subject, body)


def _e10(context: dict[str, Any]) -> RenderedEmail:
    app_name = _app_name()
    subject = t("email.E10.subject", app_name=app_name)
    body = t(
        "email.E10.body",
        full_name=context["full_name"],
        room_name=context["room_name"],
        title=context["title"],
        time_range=format_range_zh(context["start_at"], context["end_at"]),
    )
    return _compose(subject, body)


_RENDERERS: dict[str, Callable[[dict[str, Any]], RenderedEmail]] = {
    "E1": _e1,
    "E1_EXISTS": _e1_exists,
    "E2": _e2,
    "E3": _e3,
    "E4": _e4,
    "E5": _e5,
    "E6": _e6,
    "E7": _e7,
    "E8": _e8,
    "E9": _e9,
    "E10": _e10,
}


def render(kind: str, context: dict[str, Any]) -> RenderedEmail:
    """Render one email of ``kind`` from ``context``. Pure; never raises for
    a well-formed context, but a missing required key surfaces as a
    ``KeyError`` -- callers should treat that as a programming error, not a
    runtime condition to catch.
    """
    renderer = _RENDERERS.get(kind)
    if renderer is None:
        raise ValueError(f"unknown email kind: {kind!r}")
    return renderer(context)


__all__ = ["RenderedEmail", "render", "E5_PREEMPTED", "E5_ADMIN", "E5_ROOM"]
