"""Application factory: wires routes, sessions, and the operational endpoints.

Page modules register themselves through ``register(router)`` and are imported
optionally, so the app still boots when a task's pages are not present yet.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from app import __version__
from app.config import Config, load_config
from app.db import Database, create_database
from app.db.base import Connection
from app.db.migrations import migrate
from app import i18n
from app.errors import AppError, ForbiddenError
from app.settings import seed_defaults
from app.timeutil import now_utc
from app.web.framework import (
    CSRF_COOKIE,
    LANG_COOKIE,
    SESSION_COOKIE,
    Request,
    Response,
    Router,
    WSGIApp,
    issue_csrf_token,
)

#: Pages a member must still reach while they are forced to change their
#: password (spec §10.3), otherwise they would be trapped in a redirect loop.
_PASSWORD_CHANGE_ALLOWED = frozenset({"/password", "/logout", "/api/health"})

#: Page modules to load if present. Each exposes ``register(router)``.
_PAGE_MODULES = (
    "app.web.pages.auth_pages",
    "app.web.pages.member_pages",
    "app.web.pages.admin_pages",
    "app.web.pages.tutorial_pages",
)


def create_app(
    db: Database | None = None, config: Config | None = None
) -> WSGIApp:
    """Build the WSGI application."""
    config = config or load_config()
    db = db or create_database(config.database_url)

    router = Router()
    _register_core(router, db, config)

    loaded: list[str] = []
    for name in _PAGE_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        register = getattr(module, "register", None)
        if callable(register):
            register(router)
            loaded.append(name)

    def on_request(request: Request, route: Any) -> Response | None:
        request.db = db
        request.config = config
        # Decide the token before any handler renders a form. Deriving it from
        # the cookie alone meant a first-time visitor's page carried an empty
        # token while the response set a real cookie, so their first submit
        # was always refused.
        request.csrf_token = request.cookies.get(CSRF_COOKIE) or issue_csrf_token()
        request.user = _resolve_user(db, request)
        request.locale = _resolve_locale(db, request)
        # Every t() call for the rest of this request resolves in this locale.
        i18n.set_locale(request.locale)

        if request.user is not None and request.user.must_change_password:
            if request.path not in _PASSWORD_CHANGE_ALLOWED:
                if request.wants_json:
                    raise ForbiddenError("PASSWORD_CHANGE_REQUIRED")
                return Response.redirect("/password")
        return None

    def after_request(request: Request, response: Response) -> Response:
        # Remember an explicit language choice on this device.
        chosen = request.query.get("lang")
        if chosen and i18n.normalise(chosen) == request.locale:
            response.set_cookie(
                LANG_COOKIE,
                request.locale,
                http_only=False,
                secure=request.is_secure,
                max_age=60 * 60 * 24 * 365,
            )
        # Issue a CSRF token to anyone who does not have one yet. It is
        # readable by design: the check is that it is echoed back, which a
        # cross-site form cannot do.
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(
                CSRF_COOKIE,
                request.csrf_token,   # the same value the page just rendered
                http_only=False,
                secure=request.is_secure,
                max_age=60 * 60 * 12,
            )
        return response

    app = WSGIApp(router, on_request=on_request, after_request=after_request)
    app.db = db
    app.config = config
    app.loaded_pages = loaded
    return app


def _resolve_locale(db: Database, request: Request) -> str:
    """Decide which language this response is rendered in.

    Precedence: an explicit ``?lang=`` choice, then the signed-in member's
    saved preference, then this device's cookie, then the browser's
    ``Accept-Language``, then zh-TW.

    An explicit choice by a signed-in member is written through to their
    profile, because the reminder job has no browser to ask when it sends
    their mail hours later.
    """
    chosen = request.query.get("lang")
    if chosen:
        locale = i18n.normalise(chosen)
        user = request.user
        if user is not None and user.locale != locale:
            _save_locale(db, user.id, locale)
        return locale

    if request.user is not None and getattr(request.user, "locale", None):
        return i18n.normalise(request.user.locale)

    cookie = request.cookies.get(LANG_COOKIE)
    if cookie:
        return i18n.normalise(cookie)

    return i18n.from_accept_header(
        request.header("HTTP_ACCEPT_LANGUAGE") or ""
    ) or i18n.DEFAULT_LOCALE


def _save_locale(db: Database, user_id: str, locale: str) -> None:
    def work(conn: Connection) -> None:
        conn.execute(
            "UPDATE users SET locale = ?, updated_at = ? WHERE id = ?",
            (locale, now_utc(), user_id),
        )

    db.run_in_transaction(work)


def _resolve_user(db: Database, request: Request):
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        from app.services import sessions
    except ImportError:  # pragma: no cover - before Task 1 lands
        return None
    try:
        return sessions.resolve_session(db, raw)
    except AppError:
        return None


def _register_core(router: Router, db: Database, config: Config) -> None:
    """Routes owned by the foundation: root, health, and the reminder cron."""

    def root(request: Request) -> Response:
        if request.user is None:
            return Response.redirect("/login")
        return Response.redirect("/day")

    def health(request: Request) -> Response:
        """Spec §10.4. Returns 200 only when the database is reachable."""
        database_ok = True
        last_run = None
        try:

            def probe(conn: Connection) -> Any:
                return conn.query_one(
                    "SELECT started_at, finished_at, ok FROM cron_runs"
                    " WHERE job = ? ORDER BY started_at DESC LIMIT 1",
                    ("send_reminders",),
                )

            row = db.run_in_transaction(probe)
            last_run = row["finished_at"] or row["started_at"] if row else None
        except Exception:  # noqa: BLE001 - health must report, not raise
            database_ok = False

        return Response.json(
            {
                "version": __version__,
                "database": "ok" if database_ok else "unavailable",
                "last_reminder_run": last_run,
                "time": now_utc(),
            },
            200 if database_ok else 503,
        )

    def send_reminders(request: Request) -> Response:
        """Spec §9.3, invoked by the scheduled GitHub Actions workflow."""
        supplied = request.header("HTTP_X_CRON_SECRET")
        expected = config.cron_secret
        if not expected or supplied != expected:
            raise ForbiddenError("CRON_FORBIDDEN")

        from app.services import mailer

        reminders = mailer.run_reminders(db)
        delivery = mailer.send_pending(db)
        return Response.json(
            {
                "reminders": _as_dict(reminders),
                "delivery": _as_dict(delivery),
            }
        )

    router.add("GET", "/", root)
    router.add("GET", "/api/health", health)
    router.add("POST", "/api/cron/send-reminders", send_reminders, csrf_exempt=True)


def _as_dict(report: Any) -> Any:
    """Reports are dataclasses; render whatever shape they have as JSON."""
    if hasattr(report, "__dict__"):
        return {k: v for k, v in vars(report).items() if not k.startswith("_")}
    return report


def bootstrap(db: Database, config: Config) -> dict[str, Any]:
    """Migrate, seed settings, create the first admin, and seed rooms.

    Idempotent (spec §12 E4): safe to run on every deploy.
    """
    from app.services import provisioning

    def work(conn: Connection) -> dict[str, Any]:
        applied = migrate(conn)
        seeded = seed_defaults(conn)
        return {"migrations": applied, "settings_seeded": seeded}

    result = db.run_in_transaction(work)
    result.update(provisioning.seed_initial_data(db, config))
    return result


def configure_logging(level: str | None = None) -> None:
    """Send application logs to stdout.

    Render (and most hosts) collect stdout, so this is what makes a failing
    request visible in the dashboard. ``LOG_LEVEL=DEBUG`` turns up the detail
    without a redeploy of anything but the environment variable.
    """
    import logging
    import sys

    logging.basicConfig(
        level=(level or os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def build_wsgi_app():  # pragma: no cover - production entry point
    """Module-level callable for ``gunicorn app.web.app:build_wsgi_app()``."""
    configure_logging()
    config = load_config()
    db = create_database(config.database_url)
    bootstrap(db, config)
    return create_app(db, config)
