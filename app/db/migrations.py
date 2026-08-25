"""Schema migrations (spec §4).

Each migration carries the SQL for both dialects side by side so that any
divergence is visible in a single diff. Migrations are applied in version
order inside one transaction and recorded in ``schema_migrations``, which makes
re-running the deploy safe (spec §12 E4).

Dialect notes:

* UUID primary keys are stored as 36-character canonical strings in both
  backends. This avoids per-dialect casting and keeps the SQLite and Postgres
  row shapes identical.
* Timestamps are declared ``TIMESTAMPTZ`` in both. In SQLite that is a
  declared type name decoded by the converter registered in
  :mod:`app.db.base`; the stored form is a canonical ISO-8601 UTC string.
* Room opening hours are stored as integer minutes past local midnight rather
  than a ``TIME``, because the booking grid is minute arithmetic and this
  sidesteps two different ``TIME`` implementations.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.base import POSTGRES, SQLITE, Connection, execute_script


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite: str
    postgres: str

    def sql_for(self, dialect: str) -> str:
        return self.sqlite if dialect == SQLITE else self.postgres


# --- 001 initial schema -----------------------------------------------------

_COMMON_TABLES = """
CREATE TABLE users (
    id                   TEXT PRIMARY KEY,
    email                TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    full_name            TEXT NOT NULL,
    department           TEXT NOT NULL,
    phone                TEXT NOT NULL,
    level                INTEGER NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 10),
    status               TEXT NOT NULL CHECK (status IN
                             ('pending_email','pending_approval','active',
                              'rejected','suspended')),
    is_admin             BOOLEAN NOT NULL DEFAULT {false},
    must_change_password BOOLEAN NOT NULL DEFAULT {false},
    email_verified_at    TIMESTAMPTZ,
    approved_at          TIMESTAMPTZ,
    approved_by          TEXT REFERENCES users(id),
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX ux_users_email ON users (email);

CREATE TABLE rooms (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    capacity        INTEGER,
    location        TEXT,
    equipment_note  TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT {true},
    open_minutes    INTEGER,
    close_minutes   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE bookings (
    id                      TEXT PRIMARY KEY,
    room_id                 TEXT NOT NULL REFERENCES rooms(id),
    user_id                 TEXT NOT NULL REFERENCES users(id),
    title                   TEXT NOT NULL,
    start_at                TIMESTAMPTZ NOT NULL,
    end_at                  TIMESTAMPTZ NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN
                                ('confirmed','cancelled_by_user',
                                 'cancelled_by_admin','preempted')),
    level_at_booking        INTEGER NOT NULL,
    preempted_by_booking_id TEXT REFERENCES bookings(id),
    cancelled_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL,
    CHECK (end_at > start_at)
);

-- Spec §4.4: the overlap query is the hot path; only confirmed rows occupy a room.
CREATE INDEX ix_bookings_room_span ON bookings (room_id, start_at, end_at)
    WHERE status = 'confirmed';
CREATE INDEX ix_bookings_user ON bookings (user_id, start_at);

CREATE TABLE email_tokens (
    id            TEXT PRIMARY KEY,
    user_id       TEXT REFERENCES users(id),
    email         TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN
                      ('verify_email','invite','password_reset')),
    token_hash    TEXT NOT NULL,
    invited_level INTEGER CHECK (invited_level BETWEEN 1 AND 10),
    created_by    TEXT REFERENCES users(id),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX ux_email_tokens_hash ON email_tokens (token_hash);
CREATE INDEX ix_email_tokens_email ON email_tokens (email, type);

CREATE TABLE preemption_log (
    id                   TEXT PRIMARY KEY,
    victim_booking_id    TEXT NOT NULL REFERENCES bookings(id),
    winner_booking_id    TEXT NOT NULL REFERENCES bookings(id),
    victim_user_id       TEXT NOT NULL REFERENCES users(id),
    winner_user_id       TEXT NOT NULL REFERENCES users(id),
    victim_level         INTEGER NOT NULL,
    winner_level         INTEGER NOT NULL,
    room_id              TEXT NOT NULL REFERENCES rooms(id),
    occurred_at          TIMESTAMPTZ NOT NULL,
    notification_sent_at TIMESTAMPTZ
);

CREATE INDEX ix_preemption_victim ON preemption_log (victim_user_id, occurred_at);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT REFERENCES users(id)
);

CREATE TABLE email_log (
    id                  TEXT PRIMARY KEY,
    to_email            TEXT NOT NULL,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('queued','sent','failed','skipped')),
    provider_message_id TEXT,
    error               TEXT,
    related_booking_id  TEXT REFERENCES bookings(id),
    dedupe_key          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL,
    sent_at             TIMESTAMPTZ
);

-- Spec §9.3 / §12 D1: makes double-sending a reminder impossible rather than
-- merely unlikely, even if the cron endpoint is invoked twice concurrently.
CREATE UNIQUE INDEX ux_email_log_dedupe ON email_log (dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX ix_email_log_created ON email_log (created_at);

CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX ix_sessions_user ON sessions (user_id);

CREATE TABLE login_attempts (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    succeeded  BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_login_attempts_email ON login_attempts (email, created_at);

-- Spec FR-7: retained forever, admin-visible, CSV-exportable.
CREATE TABLE audit_log (
    id            TEXT PRIMARY KEY,
    actor_user_id TEXT REFERENCES users(id),
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    detail        TEXT,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_audit_created ON audit_log (created_at);

-- Spec §9.3: the admin dashboard must be able to show when the reminder job
-- last ran, because GitHub Actions schedules can silently stop.
CREATE TABLE cron_runs (
    id          TEXT PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    ok          BOOLEAN,
    detail      TEXT
);

CREATE INDEX ix_cron_runs_job ON cron_runs (job, started_at);
"""

MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="initial",
        sqlite=_COMMON_TABLES.format(false="0", true="1"),
        postgres=_COMMON_TABLES.format(false="FALSE", true="TRUE"),
    ),
]


_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL
)
"""


def applied_versions(conn: Connection) -> set[int]:
    conn.execute(_MIGRATION_TABLE)
    rows = conn.query_all("SELECT version FROM schema_migrations")
    return {int(row["version"]) for row in rows}


def migrate(conn: Connection) -> list[int]:
    """Apply pending migrations. Returns the versions applied.

    Idempotent: running it against an up-to-date database is a no-op, which is
    what makes re-running the deploy workflow safe.
    """
    from app.timeutil import now_utc  # local import avoids a cycle at import time

    done = applied_versions(conn)
    newly_applied: list[int] = []
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version in done:
            continue
        execute_script(conn, migration.sql_for(conn.dialect))
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at)"
            " VALUES (?, ?, ?)",
            (migration.version, migration.name, now_utc()),
        )
        newly_applied.append(migration.version)
    return newly_applied


__all__ = ["MIGRATIONS", "Migration", "migrate", "applied_versions", "POSTGRES", "SQLITE"]
