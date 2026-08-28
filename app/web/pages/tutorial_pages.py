"""The interactive tutorial (`/tutorial`, `/tutorial.js`).

An additive, non-core page: it renders a scripted walkthrough entirely from
canned data in :mod:`app.web.tutorial_content`, never calling
:mod:`app.services` or touching the database. Not part of the spec §12
acceptance suite.

The JS engine is served as a same-origin external file (`/tutorial.js`)
rather than inlined, so it is authorised by the site's existing
``script-src 'self' ...`` CSP directive with no hash to add.
"""

from __future__ import annotations

import json

from app.web.framework import Request, Response, Router
from app.web.html import Markup, button, div, raw
from app.web.layout import page
from app.web.tutorial_content import TRACKS, TUTORIAL_SCRIPT, TUTORIAL_STEPS


def _steps_json_for_html() -> str:
    # `</` never appears in a plain json.dumps() of these steps today, but
    # escaping it is what keeps a future caption safe to embed inside a
    # <script> tag without breaking out of it.
    return json.dumps(TUTORIAL_STEPS).replace("</", "<\\/")


def _default_track(request: Request) -> str:
    user = request.user
    if user is not None and user.is_admin:
        return "admin"
    return "member"


def _track_toggle(active: str) -> Markup:
    available_tracks = {step["track"] for step in TUTORIAL_STEPS}
    buttons = []
    for track in TRACKS:
        available = track in available_tracks
        is_current = track == active
        label = "Member walkthrough" if track == "member" else "Admin walkthrough"
        classes = ["tutorial-track-toggle", "is-active" if is_current else "secondary"]
        attrs: dict = {
            "type": "button",
            "class_": " ".join(classes),
            "data_track": track,
            "aria_pressed": "true" if is_current else "false",
        }
        if not available:
            attrs["disabled"] = True
            attrs["title"] = "Coming soon"
        buttons.append(button(label, **attrs))
    return div(*buttons, class_="tutorial-track-toggles")


def tutorial_page(request: Request) -> Response:
    track = _default_track(request)
    body = div(
        _track_toggle(track),
        div(id="tutorial-mount"),
        raw(
            f'<script type="application/json" id="tutorial-steps">'
            f"{_steps_json_for_html()}</script>"
        ),
        raw(f'<script id="tutorial-track">{track}</script>'),
        raw('<script src="/tutorial.js"></script>'),
        class_="stack",
    )
    return Response.html(page(request, "Tutorial", body))


def tutorial_script(request: Request) -> Response:
    return Response(
        TUTORIAL_SCRIPT.encode("utf-8"),
        200,
        "application/javascript; charset=utf-8",
    )


def register(router: Router) -> None:
    router.add("GET", "/tutorial", tutorial_page)
    router.add("GET", "/tutorial.js", tutorial_script)
