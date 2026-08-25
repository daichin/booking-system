"""Shared test harness.

Gives every test a freshly migrated, freshly seeded in-memory database and
factory helpers that insert rows directly. The factories deliberately bypass
the service layer so that a test for one task does not fail because another
task's code is mid-change.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
import unittest
from datetime import date, datetime, timedelta
from typing import Any

from app import models, security
from app.db import Connection, Database, create_database
from app.db.migrations import migrate
from app.settings import Settings, seed_defaults
from app.timeutil import combine_taipei, local_date, now_utc

# Tests create many accounts; full scrypt cost would dominate the runtime.
security.configure(n=1 << 10)


#: Point this at a real Postgres to run the entire suite against the
#: production backend. CI does exactly that (see .github/workflows/ci.yml);
#: without it the suite uses SQLite and needs no setup at all.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _bootstrap(conn: Connection) -> None:
    migrate(conn)
    seed_defaults(conn)


def _with_search_path(dsn: str, schema: str) -> str:
    """Pin every connection made from ``dsn`` to one schema.

    The Postgres backend opens a fresh connection per transaction, so a
    ``SET search_path`` would not survive. Putting it in the DSN makes it
    apply to all of them.
    """
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options=-csearch_path%3D{schema}"


def _make_postgres_db() -> Database:
    """A private schema on the shared Postgres, migrated and seeded.

    One schema per test keeps tests isolated from each other without the cost
    of creating a database each time.
    """
    schema = f"test_{uuid.uuid4().hex[:16]}"
    admin = create_database(TEST_DATABASE_URL)
    admin.run_in_transaction(lambda conn: conn.execute(f'CREATE SCHEMA "{schema}"'))

    db = create_database(_with_search_path(TEST_DATABASE_URL, schema))
    db.run_in_transaction(_bootstrap)
    db.test_schema = schema  # read by the teardown below
    return db


def drop_postgres_schema(schema: str) -> None:
    admin = create_database(TEST_DATABASE_URL)
    admin.run_in_transaction(
        lambda conn: conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    )


def make_db(path: str | None = None) -> Database:
    """A migrated, seeded, empty database.

    Defaults to a temporary SQLite file rather than ``:memory:``. SQLite's
    shared-cache in-memory mode reports contention as ``SQLITE_LOCKED``, which
    ``busy_timeout`` does not cover, so concurrent writers would fail instead
    of queueing -- exactly the behaviour the preemption concurrency tests need
    to exercise for real.
    """
    if TEST_DATABASE_URL:
        return _make_postgres_db()

    db = create_database(f"sqlite://{path}" if path else None)
    db.run_in_transaction(_bootstrap)
    return db


def taipei_at(days_ahead: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant at a Taipei wall-clock time, ``days_ahead`` from today.

    Booking times are expressed the way a member would think about them, so
    tests read like the acceptance scenarios in spec §12.
    """
    day: date = local_date(now_utc()) + timedelta(days=days_ahead)
    return combine_taipei(day, hour * 60 + minute)


class AppTestCase(unittest.TestCase):
    """Base class providing a database and row factories."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="booking-test-")
        self.db = make_db(os.path.join(self._tmpdir, "test.sqlite3"))
        self.addCleanup(self._drop_database)

    def _drop_database(self) -> None:
        schema = getattr(self.db, "test_schema", None)
        self.db.close()
        if schema:
            drop_postgres_schema(schema)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # --- clock ------------------------------------------------------------

    def freeze(self, moment: datetime) -> datetime:
        """Pin the application clock for the rest of this test.

        Rules expressed relative to "now" -- the preemption protection window
        especially -- would otherwise pass or fail depending on the hour the
        suite runs at. Automatically restored on teardown.
        """
        from app import timeutil

        timeutil.set_clock(lambda: moment)
        self.addCleanup(timeutil.set_clock, None)
        return moment

    def freeze_at(self, days_ahead: int, hour: int, minute: int = 0) -> datetime:
        """Freeze the clock at a Taipei wall-clock time."""
        return self.freeze(taipei_at(days_ahead, hour, minute))

    # --- access -----------------------------------------------------------

    def settings(self) -> Settings:
        return self.db.run_in_transaction(Settings.load)

    def set_setting(self, key: str, value: Any) -> None:
        from app import settings as settings_module

        self.db.run_in_transaction(
            lambda conn: settings_module.update(conn, key, value)
        )

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self.db.run_in_transaction(lambda conn: conn.query_all(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        return self.db.run_in_transaction(lambda conn: conn.query_one(sql, params))

    # --- factories --------------------------------------------------------

    def create_user(
        self,
        *,
        email: str | None = None,
        password: str = "correct horse battery",
        level: int = 1,
        status: str = models.ACTIVE,
        is_admin: bool = False,
        full_name: str = "測試使用者",
        department: str = "資訊部",
        phone: str = "1234",
        must_change_password: bool = False,
    ) -> models.User:
        user_id = models.new_id()
        address = email or f"user-{user_id[:8]}@example.com"
        now = now_utc()
        verified = now if status != models.PENDING_EMAIL else None
        approved = now if status == models.ACTIVE else None

        def insert(conn: Connection) -> None:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, full_name, department,"
                " phone, level, status, is_admin, must_change_password,"
                " email_verified_at, approved_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    security.normalise_email(address),
                    security.hash_password(password),
                    full_name,
                    department,
                    phone,
                    level,
                    status,
                    is_admin,
                    must_change_password,
                    verified,
                    approved,
                    now,
                    now,
                ),
            )

        self.db.run_in_transaction(insert)
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> models.User:
        row = self.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        assert row is not None, f"user {user_id} not found"
        return models.User.from_row(row)

    def create_room(
        self,
        *,
        name: str = "會議室 A",
        is_active: bool = True,
        open_minutes: int | None = None,
        close_minutes: int | None = None,
        capacity: int | None = 10,
    ) -> models.Room:
        room_id = models.new_id()
        now = now_utc()

        def insert(conn: Connection) -> None:
            conn.execute(
                "INSERT INTO rooms (id, name, capacity, location, equipment_note,"
                " is_active, open_minutes, close_minutes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    name,
                    capacity,
                    "3 樓",
                    "投影機",
                    is_active,
                    open_minutes,
                    close_minutes,
                    now,
                    now,
                ),
            )

        self.db.run_in_transaction(insert)
        return self.get_room(room_id)

    def get_room(self, room_id: str) -> models.Room:
        row = self.query_one("SELECT * FROM rooms WHERE id = ?", (room_id,))
        assert row is not None, f"room {room_id} not found"
        return models.Room.from_row(row)

    def create_booking(
        self,
        *,
        room: models.Room,
        user: models.User,
        start_at: datetime,
        end_at: datetime,
        title: str = "測試會議",
        status: str = models.CONFIRMED,
        level_at_booking: int | None = None,
    ) -> models.Booking:
        booking_id = models.new_id()
        now = now_utc()

        def insert(conn: Connection) -> None:
            conn.execute(
                "INSERT INTO bookings (id, room_id, user_id, title, start_at, end_at,"
                " status, level_at_booking, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    booking_id,
                    room.id,
                    user.id,
                    title,
                    start_at,
                    end_at,
                    status,
                    level_at_booking if level_at_booking is not None else user.level,
                    now,
                    now,
                ),
            )

        self.db.run_in_transaction(insert)
        return self.get_booking(booking_id)

    def get_booking(self, booking_id: str) -> models.Booking:
        row = self.query_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        assert row is not None, f"booking {booking_id} not found"
        return models.Booking.from_row(row)

    # --- assertions -------------------------------------------------------

    def assertErrorCode(self, ctx, expected: str) -> None:
        """Assert an ``AppError`` context manager caught ``expected``."""
        self.assertEqual(getattr(ctx.exception, "code", None), expected)
