#!/usr/bin/env python3
"""Operational CLI used by the deploy workflow (spec §10) and by admins.

Subcommands:

    python manage.py migrate            # run migrations + seed settings/admin/rooms
    python manage.py check-secrets      # exit non-zero, naming any missing secret
    python manage.py health --url URL   # post-deploy smoke test against /api/health
    python manage.py tutorial-build     # regenerate tutorial/offline.html
    python manage.py reset --scope S --confirm DELETE   # wipe application data

Standard library only -- this script must run before `pip install -r
requirements.txt` has necessarily happened (check-secrets in particular is
meant to fail fast, before any dependency install or network call).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


def cmd_check_secrets(args: argparse.Namespace) -> int:
    """Verify every name in ``app.config.REQUIRED_SECRETS`` is set.

    Exits non-zero and prints the exact missing names, one per line, plus a
    summary line -- this is what makes spec §12 E2 ("a missing secret fails
    the workflow with a message naming it") true.
    """
    from app.config import REQUIRED_SECRETS

    missing = [name for name in REQUIRED_SECRETS if not os.environ.get(name)]
    if missing:
        print("ERROR: missing required secret(s):", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            f"\n{len(missing)} secret(s) missing out of {len(REQUIRED_SECRETS)} required. "
            "Add them under Settings -> Secrets and variables -> Actions "
            "(see SETUP.md) and re-run the workflow.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: all {len(REQUIRED_SECRETS)} required secrets are present.")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Run migrations, seed settings, and provision the first admin + rooms.

    Idempotent (spec §12 E4): safe to run on every deploy, including the very
    first one and every one after.
    """
    from app.config import load_config
    from app.db import create_database
    from app.web.app import bootstrap

    config = load_config()
    if config.missing:
        print(
            "ERROR: cannot migrate, missing required secret(s): "
            + ", ".join(config.missing),
            file=sys.stderr,
        )
        return 1

    db = create_database(config.database_url)
    try:
        result = bootstrap(db, config)
    finally:
        db.close()

    print("Migration and seeding complete:")
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


#: The exact word the operator has to type before anything is deleted. Chosen
#: to be un-typo-able by accident: it is not "yes", it is not the scope name,
#: and it is case-sensitive.
RESET_CONFIRMATION = "DELETE"

#: What each scope leaves behind, in the operator's language rather than in
#: table names. The table-level truth lives in app/services/reset.py; this is
#: the sentence printed next to it so the two are read together.
_RESET_SUMMARY = {
    "bookings": "keeps every account, room and setting; nobody is logged out",
    "members": "keeps rooms and settings; recreates the administrator "
               "from ADMIN_EMAIL / ADMIN_INITIAL_PASSWORD",
    "all": "keeps nothing; re-seeds settings, the administrator and the "
           "three example rooms, as on a first deploy",
}


def cmd_reset(args: argparse.Namespace) -> int:
    """Wipe application data at the chosen scope. Irreversible.

    Refuses unless ``--confirm DELETE`` was passed, and refuses *before*
    touching the environment or opening a connection, so a mistyped
    invocation cannot even reach the production database.
    """
    if args.confirm != RESET_CONFIRMATION:
        print(
            "ERROR: refusing to reset: this deletes data permanently and "
            "cannot be undone.\n"
            f"       Pass --confirm {RESET_CONFIRMATION} (exactly, in capitals) "
            "to go ahead.",
            file=sys.stderr,
        )
        return 1

    from app.config import load_config
    from app.db import create_database
    from app.services import reset as reset_service

    config = load_config()
    if config.missing:
        # ADMIN_EMAIL and ADMIN_INITIAL_PASSWORD are what the administrator is
        # rebuilt from, so a reset run without them would wipe every account
        # and then have no way to create one -- locking the owner out of their
        # own site. Refuse on the whole set, as `migrate` does.
        print(
            "ERROR: cannot reset, missing required secret(s): "
            + ", ".join(config.missing),
            file=sys.stderr,
        )
        return 1

    tables = sorted(reset_service.SCOPE_TABLES[args.scope])
    print(f"Resetting scope '{args.scope}': {_RESET_SUMMARY[args.scope]}.")
    print("  Emptying: " + ", ".join(tables))
    print("  Keeping:  " + ", ".join(
        sorted(set(reset_service.ALL_TABLES) - set(tables)) + ["schema_migrations"]
    ))
    print("  This cannot be undone.")

    db = create_database(config.database_url)
    try:
        report = reset_service.reset(db, config, scope=args.scope)
    finally:
        db.close()

    print("\nRows removed:")
    width = max(len(name) for name in report.removed)
    for table, count in report.removed.items():
        print(f"  {table.ljust(width)}  {count}")
    print(f"  {'TOTAL'.ljust(width)}  {report.total_removed}")
    if report.reseeded:
        print("\nRe-seeded:")
        print(json.dumps(report.reseeded, indent=2, default=str, ensure_ascii=False))
    return 0


def _describe(error: object) -> str:
    """Turn a connection failure into something a non-expert can act on.

    "could not reach" on its own is useless at the moment it matters: a name
    that does not resolve and a port with nothing listening need completely
    different fixes.
    """
    text = str(error)
    lowered = text.lower()
    if isinstance(error, socket.gaierror) or "not known" in lowered \
            or "nodename nor servname" in lowered or "getaddrinfo" in lowered:
        return (
            f"網域名稱無法解析（{text}）。"
            " APP_BASE_URL 的主機名稱可能打錯，或該 Render 服務尚未建立。"
        )
    if isinstance(error, ConnectionRefusedError) or "refused" in lowered:
        return (
            f"連線被拒絕（{text}）。"
            " 主機存在，但沒有程式在監聽——應用程式很可能啟動失敗。"
        )
    if isinstance(error, TimeoutError) or "timed out" in lowered:
        return f"連線逾時（{text}）。主機沒有在時限內回應。"
    if "certificate" in lowered or "ssl" in lowered:
        return f"TLS/憑證問題（{text}）。"
    return text


def _probe(url: str, timeout: float) -> tuple[int | None, str]:
    """One health request.

    Returns ``(status, body)`` for any HTTP answer -- including an error
    status, which still means the service is up and talking. Returns
    ``(None, reason)`` when the request never got that far.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return None, _describe(exc.reason)
    except (TimeoutError, OSError) as exc:
        return None, _describe(exc)


def cmd_health(args: argparse.Namespace) -> int:
    """Hit ``--url`` and exit non-zero unless it is 200 with database ok.

    Used as the deploy workflow's post-deploy smoke test. Render free
    services cold-start after idling, so callers should retry around this
    (the reminders workflow already does; the deploy workflow calls this once
    right after triggering a fresh deploy, when the service is definitely
    warm from the deploy itself).
    """
    last_error = ""
    for attempt in range(1, args.retries + 1):
        status, body = _probe(args.url, args.timeout)
        if status is not None:
            break
        last_error = body
        if attempt < args.retries:
            print(
                f"  第 {attempt}/{args.retries} 次失敗：{body}",
                file=sys.stderr,
            )
            print(
                f"  {args.retry_delay:.0f} 秒後重試"
                "（Render 免費方案喚醒約需 60 秒）",
                file=sys.stderr,
            )
            time.sleep(args.retry_delay)
    else:
        print(f"\nERROR: 無法連上健康檢查端點。\n  原因：{last_error}", file=sys.stderr)
        print(
            "\n請依序檢查：\n"
            "  1. Render 儀表板上這個服務的狀態是不是 Live，"
            "若是紅色 Failed 請看它的 Logs 分頁\n"
            "  2. Secret APP_BASE_URL 是否為完整網址、以 https:// 開頭、結尾沒有多餘字元\n"
            "  3. 用瀏覽器直接開啟該網址，看是否真的打得開",
            file=sys.stderr,
        )
        return 1

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"ERROR: {args.url} did not return valid JSON:\n{body}", file=sys.stderr)
        return 1

    if status != 200 or payload.get("database") != "ok":
        print(
            f"ERROR: health check failed (status={status}, "
            f"database={payload.get('database')!r}): {body}",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {args.url} is healthy: {body}")
    return 0


def cmd_tutorial_build(args: argparse.Namespace) -> int:
    """Regenerate the standalone tutorial/offline.html from tutorial_content.py.

    Self-contained: everything (stylesheet, engine script, step data) is
    inlined so the file opens correctly via `file://` with no server, no
    network requests, and no build step. Needs no secrets/DB/network, and is
    idempotent -- re-running it after an unrelated no-op produces the same
    bytes.

    This is a content-authoring step a maintainer runs and commits after
    editing app/web/tutorial_content.py; it is not run automatically by
    `migrate` or any deploy step.
    """
    import pathlib

    from app.web.layout import STYLESHEET
    from app.web.pages.tutorial_pages import _steps_json_for_html, _track_toggle
    from app.web.tutorial_content import TUTORIAL_SCRIPT

    body = (
        '<main>\n<h1>Tutorial</h1>\n'
        + str(_track_toggle("member"))
        + '\n<div id="tutorial-mount"></div>\n'
        + '<script type="application/json" id="tutorial-steps">'
        + _steps_json_for_html()
        + "</script>\n"
        + '<script id="tutorial-track">member</script>\n'
        + "</main>\n"
    )
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Tutorial (offline)</title>\n"
        f"<style>{STYLESHEET}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        f"<script>{TUTORIAL_SCRIPT}</script>\n"
        "</body>\n</html>\n"
    )

    out_path = pathlib.Path(__file__).parent / "tutorial" / "offline.html"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"OK: wrote {out_path.relative_to(pathlib.Path(__file__).parent)} "
          f"({len(html.encode('utf-8'))} bytes)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operational CLI for the meeting room booking system."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser(
        "migrate", help="Run DB migrations and seed settings/admin/rooms."
    )
    migrate.set_defaults(func=cmd_migrate)

    check_secrets = subparsers.add_parser(
        "check-secrets",
        help="Verify all required deploy secrets are set in the environment.",
    )
    check_secrets.set_defaults(func=cmd_check_secrets)

    # The choices are spelled out rather than imported from
    # app.services.reset, so that building the parser -- which happens for
    # every subcommand, check-secrets included -- stays free of application
    # imports. tests/test_reset.py asserts the two lists have not drifted.
    reset = subparsers.add_parser(
        "reset",
        help="Permanently delete application data. Requires --confirm DELETE.",
    )
    reset.add_argument(
        "--scope",
        required=True,
        choices=("bookings", "members", "all"),
        help="bookings: bookings and history only. "
             "members: also every account, including administrators. "
             "all: everything, back to a freshly deployed system.",
    )
    reset.add_argument(
        "--confirm",
        default="",
        help=f"Must be the literal string {RESET_CONFIRMATION!r}. "
             "Nothing is deleted without it.",
    )
    reset.set_defaults(func=cmd_reset)

    health = subparsers.add_parser(
        "health", help="Smoke-test a deployed instance's /api/health endpoint."
    )
    health.add_argument("--url", required=True, help="Full URL of /api/health.")
    health.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Request timeout in seconds (default: 15).",
    )
    health.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Attempts before giving up (default: 1). Raise this when the "
             "service may be cold-starting.",
    )
    health.add_argument(
        "--retry-delay",
        type=float,
        default=15.0,
        help="Seconds between attempts (default: 15).",
    )
    health.set_defaults(func=cmd_health)

    tutorial_build = subparsers.add_parser(
        "tutorial-build",
        help="Regenerate the standalone tutorial/offline.html from tutorial_content.py.",
    )
    tutorial_build.set_defaults(func=cmd_tutorial_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
