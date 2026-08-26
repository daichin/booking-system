#!/usr/bin/env python3
"""Operational CLI used by the deploy workflow (spec §10) and by admins.

Subcommands:

    python manage.py migrate            # run migrations + seed settings/admin/rooms
    python manage.py check-secrets      # exit non-zero, naming any missing secret
    python manage.py health --url URL   # post-deploy smoke test against /api/health

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
