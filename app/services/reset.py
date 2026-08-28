"""Destructive data reset, run from the GitHub Actions "Reset" button.

The owner needs a way to clear the site out -- after a pilot, at the start of
a new term, or when the example data has served its purpose. There is no
database console in the normal path (spec C3: a phone and the GitHub web UI),
so the wipe has to be an operation the application itself offers.

Three scopes, nested: each one removes everything the smaller one removes,
plus more.

======================  ========  =========  =====
table                   bookings  members    all
======================  ========  =========  =====
preemption_log          wiped     wiped      wiped
email_log               wiped     wiped      wiped
audit_log               wiped     wiped      wiped
login_attempts          wiped     wiped      wiped
cron_runs               wiped     wiped      wiped
bookings                wiped     wiped      wiped
email_tokens            kept      wiped      wiped
sessions                kept      wiped      wiped
users                   kept      wiped      wiped
settings                kept      kept       wiped
rooms                   kept      kept       wiped
schema_migrations       kept      kept       kept
======================  ========  =========  =====

Consequences worth knowing before pressing the button:

* ``bookings`` keeps ``sessions``, so nobody is logged out, and keeps
  ``email_tokens``, so an invitation or password-reset link already in
  somebody's inbox still works. It does blank ``cron_runs``, which empties the
  dashboard's "last reminder job ran at" until the next quarter-hour.
* ``members`` deletes administrators too -- the request was explicit about
  that -- so the administrator is recreated from ``ADMIN_EMAIL`` /
  ``ADMIN_INITIAL_PASSWORD`` afterwards, back to a forced password change.
* ``all`` additionally restores the default settings and the example rooms,
  leaving the system indistinguishable from a first deploy.

``schema_migrations`` is never touched: this resets *data*, not the schema. If
it were emptied the next deploy would try to re-apply migration 001 against
tables that already exist and fail.

The whole wipe is one transaction. A failure part-way through -- a foreign key
nobody anticipated, a dropped connection -- must leave the site exactly as it
was rather than half-erased, which is the one outcome worse than not resetting
at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import Config
from app.db.base import Connection, Database
from app.services import audit, provisioning
from app.settings import seed_defaults

SCOPE_BOOKINGS = "bookings"
SCOPE_MEMBERS = "members"
SCOPE_ALL = "all"

#: Ordered least to most destructive. The CLI and the workflow offer them in
#: this order so the first choice is the safest one.
SCOPES = (SCOPE_BOOKINGS, SCOPE_MEMBERS, SCOPE_ALL)

#: Audit action written for every reset. Deliberately not in
#: :mod:`app.services.audit` alongside the day-to-day actions: this one is not
#: something the application does to itself, it is an operator intervention.
DATA_RESET = "data_reset"

#: Every application table, ordered child before parent, which is the order
#: the deletes run in. Nothing here ever removes a row that a still-present
#: row points at: the three tables carrying a user_id-shaped column
#: (audit_log, sessions, email_tokens) and settings.updated_by all precede
#: users, bookings precedes rooms and users, and preemption_log and email_log
#: precede bookings. Names come from this literal and never from input, which
#: is what makes interpolating them into SQL below safe.
ALL_TABLES = (
    "preemption_log",
    "email_log",
    "audit_log",
    "login_attempts",
    "cron_runs",
    "bookings",
    "email_tokens",
    "sessions",
    "settings",
    "users",
    "rooms",
)

#: What happened, not who anyone is: the record of past activity.
_HISTORY = frozenset(
    {"preemption_log", "email_log", "audit_log", "login_attempts",
     "cron_runs", "bookings"}
)

#: Who can get in.
_IDENTITY = frozenset({"email_tokens", "sessions", "users"})

#: How the site is configured.
_CONFIGURATION = frozenset({"settings", "rooms"})

SCOPE_TABLES: dict[str, frozenset[str]] = {
    SCOPE_BOOKINGS: _HISTORY,
    SCOPE_MEMBERS: _HISTORY | _IDENTITY,
    SCOPE_ALL: _HISTORY | _IDENTITY | _CONFIGURATION,
}


@dataclass(frozen=True)
class ResetReport:
    scope: str
    #: Table name -> rows deleted. Every wiped table appears, including the
    #: ones that were already empty, so the operator can see the full extent
    #: of what ran rather than only what happened to have data in it.
    removed: dict[str, int] = field(default_factory=dict)
    #: Tables this scope left alone, in wipe order.
    kept: tuple[str, ...] = ()
    #: Outcome of the re-seed, as returned by
    #: :func:`app.services.provisioning.seed_initial_data`. Empty for the
    #: ``bookings`` scope, which seeds nothing.
    reseeded: dict[str, Any] = field(default_factory=dict)

    @property
    def total_removed(self) -> int:
        return sum(self.removed.values())


def reset(db: Database, config: Config, *, scope: str) -> ResetReport:
    """Wipe the tables named by ``scope`` and re-seed what that scope removes.

    Callers are responsible for confirming with a human first; this function
    does what it is told.
    """
    if scope not in SCOPE_TABLES:
        raise ValueError(f"unknown reset scope {scope!r}; expected one of {SCOPES}")

    tables = SCOPE_TABLES[scope]

    def work(conn: Connection) -> dict[str, int]:
        if "users" in tables and "settings" not in tables:
            # The members scope keeps the settings but not the people who
            # edited them, and settings.updated_by is a foreign key into
            # users. Clearing it is the difference between this transaction
            # committing and failing on the DELETE below.
            conn.execute("UPDATE settings SET updated_by = NULL")

        removed: dict[str, int] = {}
        for table in ALL_TABLES:
            if table not in tables:
                continue
            removed[table] = int(conn.query_value(f"SELECT COUNT(*) FROM {table}"))
            conn.execute(f"DELETE FROM {table}")

        if scope == SCOPE_ALL:
            # The settings table was just emptied and the application reads
            # every business rule from it, so it must be repopulated before
            # anything else runs -- including the seeding below, which reads
            # nothing from it today but would be a silent trap if it ever did.
            seed_defaults(conn)

        # Written *after* the deletes, deliberately. audit_log is itself one
        # of the tables being wiped in all three scopes, so an entry recorded
        # before the wipe would be deleted by it and the reset would leave no
        # trace of itself at all. Recorded here, inside the same transaction,
        # it is the first row of the new trail and it rolls back with the
        # wipe if anything fails. actor_id is NULL because the operator is a
        # workflow run, not a logged-in user -- and under the wider scopes
        # there is no users row left to point at anyway.
        audit.record(
            conn,
            actor_id=None,
            action=DATA_RESET,
            detail={"scope": scope, "removed": removed},
        )
        return removed

    removed = db.run_in_transaction(work)

    reseeded: dict[str, Any] = {}
    if scope in (SCOPE_MEMBERS, SCOPE_ALL):
        # A separate transaction: seed_initial_data owns its own, and it is
        # the recovery path as well -- if this half fails, re-running
        # `manage.py migrate` finishes the job without touching the wipe.
        reseeded = provisioning.seed_initial_data(db, config)

    return ResetReport(
        scope=scope,
        removed=removed,
        kept=tuple(table for table in ALL_TABLES if table not in tables),
        reseeded=reseeded,
    )


__all__ = [
    "ALL_TABLES",
    "DATA_RESET",
    "SCOPES",
    "SCOPE_ALL",
    "SCOPE_BOOKINGS",
    "SCOPE_MEMBERS",
    "SCOPE_TABLES",
    "ResetReport",
    "reset",
]
