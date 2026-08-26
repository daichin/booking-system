"""Page shell, navigation, and the single stylesheet.

Mobile-responsive is a requirement, not a nice-to-have (spec §8): the owner and
many members will use this from a phone. The layout is therefore mobile-first,
with the day grid switching from stacked cards to side-by-side columns only
once there is room.
"""

from __future__ import annotations

from typing import Any

from app.i18n import t
from app.web.enhance import SCRIPT
from app.web.framework import CSRF_COOKIE, CSRF_FIELD, Request
from app.web.html import Markup, a, div, esc, footer, form, h1, hidden, join, li
from app.web.html import main as main_el
from app.web.html import nav, p, raw, span, ul

STYLESHEET = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1c2430; --muted: #5d6b7a;
  --line: #dfe4ea; --accent: #1f6feb; --accent-ink: #ffffff;
  --ok: #0f7b3f; --ok-bg: #e6f4ec; --warn: #8a5a00; --warn-bg: #fdf3df;
  --err: #b3261e; --err-bg: #fdecea; --mine: #e8f0fe; --other: #eef1f4;
  --radius: 10px;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei",
               system-ui, -apple-system, sans-serif;
  font-size: 16px; line-height: 1.6;
}
a { color: var(--accent); }
.site-header {
  background: var(--panel); border-bottom: 1px solid var(--line);
  position: sticky; top: 0; z-index: 10;
}
.site-header .bar {
  max-width: 1100px; margin: 0 auto; padding: 0.75rem 1rem;
  display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; align-items: center;
}
.brand { font-weight: 700; font-size: 1.05rem; text-decoration: none; color: var(--ink); }
.site-nav ul { list-style: none; display: flex; flex-wrap: wrap; gap: 0.25rem 0.75rem; margin: 0; padding: 0; }
.site-nav a { text-decoration: none; padding: 0.35rem 0.6rem; border-radius: 6px; display: block; }
.site-nav a[aria-current="page"] { background: var(--mine); font-weight: 600; }
.spacer { margin-left: auto; display: flex; align-items: center; gap: 0.5rem; }
.account-name { color: var(--muted); font-size: 0.9rem; max-width: 8em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* On a phone the header had four groups and wrapped to four rows, eating the
   screen before any content appeared. Below 640px the title and the member's
   name are dropped and the nav scrolls sideways instead of wrapping, which
   holds the whole header to two rows however many nav items there are. */
@media (max-width: 640px) {
  .site-header .bar { padding: 0.5rem 0.75rem; gap: 0.4rem 0.6rem; }
  .brand { display: none; }
  .account-name { display: none; }
  .site-nav { flex: 1 1 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .site-nav ul { flex-wrap: nowrap; gap: 0.25rem; }
  .site-nav a { white-space: nowrap; padding: 0.3rem 0.5rem; }
  .spacer { margin-left: 0; }
  .lang-switch { margin-left: auto; }
}
main { max-width: 1100px; margin: 0 auto; padding: 1rem; }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 1rem; margin-bottom: 1rem;
}
h1 { font-size: 1.35rem; margin: 0 0 0.75rem; }
h2 { font-size: 1.1rem; margin: 1.25rem 0 0.5rem; }
.muted { color: var(--muted); }
.notice { padding: 0.7rem 0.9rem; border-radius: var(--radius); margin-bottom: 1rem; border: 1px solid transparent; }
.notice-info { background: var(--mine); border-color: #cbd9f2; }
.notice-success { background: var(--ok-bg); border-color: #bfe0cd; color: var(--ok); }
.notice-warning { background: var(--warn-bg); border-color: #ecd9a8; color: var(--warn); }
.notice-error { background: var(--err-bg); border-color: #f2c4c0; color: var(--err); }
.field { margin-bottom: 0.9rem; }
label { display: block; font-weight: 600; margin-bottom: 0.25rem; font-size: 0.95rem; }
input, select, textarea {
  width: 100%; padding: 0.6rem 0.7rem; font: inherit; color: inherit;
  background: #fff; border: 1px solid var(--line); border-radius: 8px;
}
input:focus, select:focus, textarea:focus, button:focus-visible {
  outline: 3px solid #bcd3fb; outline-offset: 1px; border-color: var(--accent);
}
input[readonly] { background: #f1f3f5; color: var(--muted); }
.help { color: var(--muted); display: block; margin-top: 0.2rem; }
button, .btn {
  font: inherit; font-weight: 600; cursor: pointer; border-radius: 8px;
  padding: 0.6rem 1rem; border: 1px solid var(--accent);
  background: var(--accent); color: var(--accent-ink); text-decoration: none;
  display: inline-block; text-align: center;
}
button.secondary, .btn.secondary { background: #fff; color: var(--accent); }
button.danger, .btn.danger { background: var(--err); border-color: var(--err); color: #fff; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
th, td { text-align: start; padding: 0.55rem 0.6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: #f2f4f7; font-size: 0.85rem; letter-spacing: 0.02em; }
.tag { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.8rem; background: var(--other); }
.tag-confirmed { background: var(--ok-bg); color: var(--ok); }
.tag-preempted { background: var(--warn-bg); color: var(--warn); }
.tag-cancelled { background: var(--err-bg); color: var(--err); }

/* Jumping between rooms (or weeks) without scrolling the whole page. A
   <details> behaves like a dropdown but is native HTML, so this needs no
   JavaScript. */
/* No `scroll-behavior: smooth` here. With it set on the root, a fragment
   navigation to an element at the very top of the document silently does
   nothing -- which is what made the back-to-top button appear dead even
   after its target was moved off the sticky header. Fragment jumps are
   therefore instant, which always works, and the script animates the two
   places where animation is worth having. */
.jump { margin-bottom: 0.9rem; }
.jump > summary {
  cursor: pointer; display: inline-block; list-style: none;
  padding: 0.5rem 0.9rem; border: 1px solid var(--line); border-radius: 8px;
  background: var(--panel); font-weight: 600; font-size: 0.95rem;
}
.jump > summary::-webkit-details-marker { display: none; }
.jump > summary::after { content: " ▾"; color: var(--muted); }
.jump[open] > summary::after { content: " ▴"; }
.jump-list {
  list-style: none; margin: 0.4rem 0 0; padding: 0.4rem;
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; max-height: 18rem; overflow-y: auto;
  display: grid; gap: 0.15rem; grid-template-columns: 1fr;
}
@media (min-width: 600px) { .jump-list { grid-template-columns: 1fr 1fr; } }
.jump-list a {
  display: block; padding: 0.5rem 0.7rem; border-radius: 6px;
  text-decoration: none; color: var(--ink);
}
.jump-list a:hover { background: var(--mine); }
.jump-list a[aria-current="true"] { background: var(--mine); font-weight: 700; }

/* Day grid: stacked on a phone, columns once there is width.
   Column counts are capped rather than auto-fitted. auto-fit packed five
   rooms onto a wide screen, leaving each one too narrow for a booking title
   and a slot action to sit side by side. */
.day-grid { display: grid; gap: 0.75rem; grid-template-columns: 1fr; }
@media (min-width: 700px) { .day-grid { grid-template-columns: repeat(2, 1fr); } }
@media (min-width: 1000px) {
  .day-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
  .day-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }
}
.room-column {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden;
  /* Clear the sticky header when jumped to from the room list. */
  scroll-margin-top: 6.5rem;
}
.room-column h3 {
  margin: 0; padding: 0.6rem 0.75rem; background: #f2f4f7; font-size: 1rem;
  border-bottom: 1px solid var(--line);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.slot-list { list-style: none; margin: 0; padding: 0; }

/* Every row is exactly the same height. Columns are independent lists, so a
   booking title that wrapped onto a second line used to push that column out
   of step with the ones beside it and the times stopped lining up across the
   grid. Overflowing text is clipped instead; the full text is in the row's
   title attribute. */
.slot {
  display: flex; gap: 0.5rem; padding: 0 0.75rem; align-items: center;
  border-bottom: 1px solid #eef1f4; height: 2.75rem;
  /* Picking a start reloads the page and lands on this row. Clear both the
     sticky header and the sticky selection banner that appears above it,
     otherwise the row you just chose sits underneath them. */
  scroll-margin-top: 8.5rem;
}
.slot-time {
  flex: none; font-variant-numeric: tabular-nums;
  color: var(--muted); font-size: 0.85rem; min-width: 5.5em;
}
.slot-detail {
  flex: 1 1 auto; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.slot-free { color: var(--muted); }
.slot.is-booked { background: var(--other); }
.slot.is-mine { background: var(--mine); }
.slot-title { font-weight: 600; }
.slot-owner { color: var(--muted); font-size: 0.85rem; margin-inline-start: 0.4em; }
.legend { display: flex; flex-wrap: wrap; gap: 0.75rem; font-size: 0.85rem; color: var(--muted); margin-bottom: 0.75rem; }
.legend span::before { content: ""; display: inline-block; width: 0.8em; height: 0.8em; border-radius: 3px; margin-inline-end: 0.35em; vertical-align: middle; }
.legend .k-mine::before { background: var(--mine); border: 1px solid #cbd9f2; }
.legend .k-other::before { background: var(--other); border: 1px solid var(--line); }
.legend .k-free::before { background: #fff; border: 1px solid var(--line); }
/* Two-click slot picking. The "selecting" state must look different enough
   that it is obvious the second click means something else. */
.date-bar { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; margin-bottom: 0.5rem; }
.date-bar .btn { padding: 0.45rem 0.8rem; font-size: 0.9rem; }
.date-jump { display: flex; gap: 0.35rem; align-items: center; }
.date-jump input { width: auto; min-width: 9.5rem; padding: 0.4rem 0.5rem; }
.date-jump button { padding: 0.45rem 0.8rem; font-size: 0.9rem; }
.day-heading { font-weight: 700; font-size: 1.05rem; margin: 0.25rem 0 0.75rem; }
.selection-banner {
  display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center;
  justify-content: space-between; background: var(--mine);
  border: 2px solid var(--accent); border-radius: var(--radius);
  padding: 0.75rem 1rem; margin-bottom: 0.9rem; position: sticky; top: 3.6rem; z-index: 5;
}
.selection-text { font-weight: 600; }
.room-column.is-selecting { outline: 2px solid var(--accent); }
.slot-action {
  margin-inline-start: auto; white-space: nowrap; text-decoration: none;
  font-size: 0.85rem; font-weight: 600; padding: 0.2rem 0.55rem;
  border: 1px solid var(--line); border-radius: 999px; background: #fff;
}
.slot-action:hover { background: var(--mine); border-color: var(--accent); }
.slot.is-start { background: var(--accent); }
.slot.is-start .slot-time, .slot.is-start .slot-free, .slot.is-start .slot-owner { color: #e8f0fe; }
.slot.is-start .slot-title { color: #fff; }
.slot.is-in-range { background: var(--mine); }
.slot.is-unreachable { opacity: 0.45; }
.manual-entry { margin-top: 1rem; }
.manual-entry > summary { cursor: pointer; color: var(--muted); font-size: 0.9rem; padding: 0.5rem 0; }

/* One-click meeting subjects. Each chip is its own submit button. */
.chips { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-bottom: 0.9rem; }
.chip {
  padding: 0.5rem 0.9rem; border-radius: 999px; font-size: 0.95rem;
  border: 1px solid var(--accent); background: var(--accent); color: #fff;
}
.chip.secondary { background: #fff; color: var(--accent); }

.confirm-panel { border: 2px solid var(--warn); background: var(--warn-bg); border-radius: var(--radius); padding: 1rem; margin-bottom: 1rem; }
.confirm-panel h2 { margin-top: 0; color: var(--warn); }
.victim-list { margin: 0.5rem 0 0; padding-inline-start: 1.2rem; }
.site-footer { max-width: 1100px; margin: 0 auto; padding: 1rem; color: var(--muted); font-size: 0.85rem; }
.inline-form { display: inline; }
.stack > * + * { margin-top: 0.75rem; }
.grid-2 { display: grid; gap: 0.75rem; grid-template-columns: 1fr; }
@media (min-width: 640px) { .grid-2 { grid-template-columns: 1fr 1fr; } }
.skip-link { position: absolute; left: -9999px; }
.skip-link:focus { left: 1rem; top: 0.5rem; background: #fff; padding: 0.5rem; z-index: 20; }

/* Back to top. A plain anchor, so it works with no JavaScript and the
   scrolling is animated by the same scroll-behavior rule as the jump lists
   (and is likewise disabled for reduced-motion readers). Always visible:
   fading it in after a scroll threshold is the one variant that would need
   JavaScript. */
.to-top {
  position: fixed; inset-inline-end: 1rem; bottom: 1rem; z-index: 15;
  width: 3rem; height: 3rem; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--panel); color: var(--accent);
  border: 1px solid var(--line); box-shadow: 0 2px 10px rgba(28, 36, 48, 0.18);
  text-decoration: none; font-size: 1.3rem; line-height: 1; opacity: 0.92;
}
.to-top:hover { opacity: 1; background: var(--mine); border-color: var(--accent); }
/* Keep it clear of the last column's slot actions on a narrow screen. */
@media (max-width: 640px) { main { padding-bottom: 4.5rem; } }
@media print { .to-top { display: none; } }
/* Only applied once the script runs, so with no JavaScript the button simply
   stays visible rather than disappearing for good. */
.to-top.is-hidden { opacity: 0; visibility: hidden; pointer-events: none; }
.to-top { transition: opacity 0.18s ease, visibility 0.18s ease; }
@media (prefers-reduced-motion: reduce) { .to-top { transition: none; } }
.jump-select {
  width: auto; min-width: 12rem; max-width: 100%;
  margin-bottom: 0.9rem; font-weight: 600;
}
"""


def _language_links(request: Request) -> Markup:
    """Switch language without losing the page you are on.

    Rendered as plain links carrying ``?lang=``, so it works with no
    JavaScript and can be bookmarked.
    """
    from urllib.parse import urlencode

    from app.i18n import AVAILABLE_LOCALES

    links = []
    for code, label in AVAILABLE_LOCALES:
        if code == request.locale:
            links.append(span(label, class_="lang-current"))
            continue
        query = {k: v for k, v in request.query.items() if k != "lang"}
        query["lang"] = code
        links.append(a(label, href=f"{request.path}?{urlencode(query)}", rel="nofollow"))
    return div(*links, class_="lang-switch")


def _nav_items(request: Request) -> list[tuple[str, str]]:
    """Navigation appropriate to the viewer's role (spec §3)."""
    user = request.user
    if user is None:
        return [("/login", t("nav.login")), ("/register", t("nav.register"))]

    items = [
        ("/day", t("nav.day")),
        ("/week", t("nav.week")),
        ("/my", t("nav.my_bookings")),
    ]
    if user.is_admin:
        items.append(("/admin", t("nav.admin")))
    return items


def page(
    request: Request,
    title: str,
    *content: Any,
    banners: list[Any] | None = None,
) -> str:
    """Render a complete HTML document."""
    user = request.user
    current = request.path

    links = [
        li(a(caption, href=href, aria_current="page" if current == href else None))
        for href, caption in _nav_items(request)
    ]

    language = _language_links(request)

    if user is not None:
        account = div(
            span(f"{user.full_name}", class_="account-name"),
            form(
                hidden(CSRF_FIELD, request.cookies.get(CSRF_COOKIE, "")),
                Markup('<button class="secondary" type="submit">')
                + esc(t("nav.logout"))
                + raw("</button>"),
                method="post",
                action="/logout",
                class_="inline-form",
            ),
            class_="spacer",
        )
    else:
        account = span("", class_="spacer")

    body = join(
        [
            # The back-to-top target. It must NOT be the header: that is
            # sticky, so once you scroll it is already painted at the top of
            # the viewport and scrolling to it moves nothing at all -- the
            # button looked broken because it genuinely did nothing.
            raw('<span id="top"></span>'),
            Markup('<a class="skip-link" href="#main">') + esc(t("nav.skip")) + raw("</a>"),
            Markup('<header class="site-header">')
            + div(
                a(t("app.name"), href="/", class_="brand"),
                nav(ul(*links), class_="site-nav"),
                account,
                language,
                class_="bar",
            )
            + raw("</header>"),
            main_el(
                join(banners or []),
                h1(title),
                join(content),
                id="main",
            ),
            footer(
                p(t("app.timezone_note"), class_="muted"),
                class_="site-footer",
            ),
            a(
                "↑",
                href="#top",
                class_="to-top",
                aria_label=t("nav.back_to_top"),
                title=t("nav.back_to_top"),
            ),
        ]
    )

    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant-TW">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)} - {esc(t('app.name'))}</title>\n"
        f"<style>{STYLESHEET}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        # Enhancements only. Every interaction it touches already works
        # without it, so a blocked or broken script costs nothing.
        f"<script>{SCRIPT}</script>\n"
        "</body>\n</html>"
    )


def bare_page(title: str, *content: Any) -> str:
    """A shell with no navigation, for token landing pages and errors."""
    body = main_el(h1(title), join(content), id="main")
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant-TW">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)} - {esc(t('app.name'))}</title>\n"
        f"<style>{STYLESHEET}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>"
    )


def error_page(request: Request, status: int, message: str) -> str:
    """Rendered for any :class:`AppError` that reaches the web layer."""
    return bare_page(
        t("error.page_title"),
        div(
            p(message),
            p(a(t("nav.home"), href="/")),
            class_="panel",
        ),
    )
