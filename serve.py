#!/usr/bin/env python3
"""Run the app locally with the SQLite backend -- no Postgres, no pip install.

    python serve.py

This is for a developer's machine, not production. Production runs under
`gunicorn` against Postgres (see `Procfile`, `requirements.txt`); this script
uses only the standard library (`wsgiref`) so it works right after cloning the
repository with nothing installed.

Configuration is picked up from the environment exactly as in production
(`app.config.load_config`), but sensible local defaults are filled in first so
the site works with zero setup:

* ``DATABASE_URL`` defaults to a file-backed SQLite database in ``./data/``
  (persists between runs; delete the file to start fresh).
* ``EMAIL_TRANSPORT`` defaults to ``fake`` so no outbound email is attempted.
* ``SESSION_SECRET`` / ``CRON_SECRET`` get throwaway local values if unset.
* ``ADMIN_EMAIL`` / ``ADMIN_INITIAL_PASSWORD`` default to a printed-out demo
  admin login so a fresh clone has something to log in with immediately.

None of these defaults are used in production: the deploy workflow always
supplies every secret explicitly (see `manage.py check-secrets`).
"""

from __future__ import annotations

import os
import sys
from wsgiref.simple_server import make_server

_DEFAULTS = {
    "DATABASE_URL": "sqlite://data/dev.sqlite3",
    "EMAIL_TRANSPORT": "fake",
    "APP_BASE_URL": "http://127.0.0.1:8000",
    "SESSION_SECRET": "local-dev-session-secret-not-for-production",
    "CRON_SECRET": "local-dev-cron-secret-not-for-production",
    "ADMIN_EMAIL": "admin@example.com",
    "ADMIN_INITIAL_PASSWORD": "ChangeMe123!",
    # EMAIL_PROVIDER_API_KEY / EMAIL_FROM_ADDRESS are required by
    # REQUIRED_SECRETS but never actually used while EMAIL_TRANSPORT=fake.
    "EMAIL_PROVIDER_API_KEY": "local-dev-not-used",
    "EMAIL_FROM_ADDRESS": "noreply@example.com",
}


def main() -> int:
    for key, value in _DEFAULTS.items():
        os.environ.setdefault(key, value)

    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    # Import after the environment defaults are set, so `load_config()` (run
    # both here and again inside `create_app`) sees them.
    from app.config import load_config
    from app.db import create_database
    from app.web.app import bootstrap, configure_logging, create_app

    # Same request log locally as in production, so a problem reproduced here
    # looks identical to what the Render dashboard shows.
    configure_logging()

    config = load_config()
    db = create_database(config.database_url)
    bootstrap(db, config)
    app = create_app(db, config)

    host, port = "127.0.0.1", config.port
    httpd = make_server(host, port, app)
    print(f"Serving on http://{host}:{port}  (Ctrl+C to stop)")
    print(f"Demo admin login: {config.admin_email} / {config.admin_initial_password}")
    print("(the app will force a password change on first login)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
