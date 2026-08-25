"""Foundation tests: storage round-tripping, migrations, settings, time rules."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from app import models, security
from app.db.migrations import applied_versions, migrate
from app.errors import AppError
from app.settings import DEFAULTS, Settings, coerce, seed_defaults
from app.timeutil import (
    TAIPEI,
    UTC,
    format_range_zh,
    is_aligned,
    parse_hhmm,
    to_taipei,
)
from tests.support import AppTestCase, taipei_at


class MigrationTests(AppTestCase):
    def test_migrations_are_recorded(self):
        versions = self.db.run_in_transaction(applied_versions)
        self.assertIn(1, versions)

    def test_migrating_twice_is_a_no_op(self):
        # Spec §12 E4: re-running the deploy must be safe.
        again = self.db.run_in_transaction(migrate)
        self.assertEqual(again, [])

    def test_seeding_twice_inserts_nothing_new(self):
        inserted = self.db.run_in_transaction(seed_defaults)
        self.assertEqual(inserted, 0)


class StorageRoundTripTests(AppTestCase):
    def test_timestamps_come_back_as_aware_utc(self):
        user = self.create_user()
        self.assertIsNotNone(user.created_at.tzinfo)
        self.assertEqual(user.created_at.utcoffset(), timedelta(0))

    def test_booleans_round_trip(self):
        admin = self.create_user(is_admin=True)
        member = self.create_user(is_admin=False)
        self.assertIs(admin.is_admin, True)
        self.assertIs(member.is_admin, False)

    def test_nullable_columns_stay_null(self):
        room = self.create_room(open_minutes=None, close_minutes=None)
        self.assertIsNone(room.open_minutes)
        self.assertIsNone(room.close_minutes)

    def test_booking_span_survives_the_round_trip(self):
        room = self.create_room()
        user = self.create_user()
        start, end = taipei_at(1, 14), taipei_at(1, 15, 30)
        booking = self.create_booking(
            room=room, user=user, start_at=start, end_at=end
        )
        self.assertEqual(booking.start_at, start)
        self.assertEqual(booking.end_at, end)
        self.assertEqual(to_taipei(booking.start_at).hour, 14)

    def test_title_that_looks_like_a_timestamp_stays_text(self):
        # The SQLite backend decodes by declared column type, so a title that
        # happens to look like an ISO timestamp must not become a datetime.
        room, user = self.create_room(), self.create_user()
        booking = self.create_booking(
            room=room,
            user=user,
            start_at=taipei_at(1, 9),
            end_at=taipei_at(1, 10),
            title="2026-09-03T14:00:00+00:00",
        )
        self.assertIsInstance(booking.title, str)


class SettingsTests(AppTestCase):
    def test_defaults_match_the_spec(self):
        s = self.settings()
        self.assertEqual(s.slot_minutes, 30)
        self.assertEqual(s.max_booking_minutes, 240)
        self.assertEqual(s.booking_horizon_days, 60)
        self.assertEqual(s.preemption_protection_minutes, 120)
        self.assertEqual(s.reminder_lead_minutes, 60)
        self.assertEqual(s.daily_email_cap, 280)
        self.assertTrue(s.reminders_enabled)
        self.assertEqual(s.default_open_minutes, parse_hhmm("08:00"))
        self.assertEqual(s.default_close_minutes, parse_hhmm("22:00"))

    def test_quota_covers_every_level(self):
        s = self.settings()
        for level in range(1, 11):
            self.assertIsInstance(s.quota_for(level), int)

    def test_zero_quota_means_unlimited(self):
        quotas = dict(DEFAULTS["quota_by_level"])
        quotas["4"] = 0
        self.set_setting("quota_by_level", quotas)
        self.assertIsNone(self.settings().quota_for(4))

    def test_updates_are_visible_immediately(self):
        self.set_setting("preemption_protection_minutes", 0)
        self.assertEqual(self.settings().preemption_protection_minutes, 0)

    def test_invalid_values_are_rejected(self):
        for key, bad in [
            ("slot_minutes", "abc"),
            ("booking_horizon_days", 0),
            ("default_open_time", "25:00"),
            ("preemption_protection_minutes", -5),
        ]:
            with self.subTest(key=key), self.assertRaises(AppError) as ctx:
                coerce(key, bad)
            self.assertEqual(ctx.exception.code, "INVALID_SETTING")

    def test_quota_must_cover_all_ten_levels(self):
        with self.assertRaises(AppError):
            coerce("quota_by_level", {"1": 3})


class TimeRuleTests(AppTestCase):
    def test_thirty_minute_grid(self):
        self.assertTrue(is_aligned(taipei_at(1, 14, 0), 30))
        self.assertTrue(is_aligned(taipei_at(1, 14, 30), 30))
        self.assertFalse(is_aligned(taipei_at(1, 14, 10), 30))

    def test_display_is_taipei_time(self):
        # 06:00 UTC is 14:00 in Taipei.
        instant = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
        self.assertEqual(to_taipei(instant).hour, 14)
        self.assertEqual(to_taipei(instant).tzinfo, TAIPEI)

    def test_range_rendering_carries_the_timezone_label(self):
        start = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
        end = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
        rendered = format_range_zh(start, end)
        self.assertIn("2026-09-03", rendered)
        self.assertIn("14:00", rendered)
        self.assertIn("15:00", rendered)
        self.assertIn("(台北時間)", rendered)
        self.assertIn("(四)", rendered)  # 2026-09-03 is a Thursday


class SecurityTests(AppTestCase):
    def test_password_round_trip(self):
        stored = security.hash_password("correct horse battery")
        self.assertTrue(security.verify_password("correct horse battery", stored))
        self.assertFalse(security.verify_password("wrong password", stored))

    def test_each_hash_uses_a_fresh_salt(self):
        first = security.hash_password("same password here")
        second = security.hash_password("same password here")
        self.assertNotEqual(first, second)

    def test_malformed_hash_does_not_raise(self):
        self.assertFalse(security.verify_password("x", "not-a-hash"))

    def test_password_rules(self):
        self.assertEqual(security.password_problem("short"), "PASSWORD_TOO_SHORT")
        self.assertEqual(security.password_problem("password"), "PASSWORD_TOO_COMMON")
        self.assertIsNone(security.password_problem("a decent passphrase"))

    def test_tokens_are_stored_hashed(self):
        raw, hashed = security.new_token()
        self.assertNotEqual(raw, hashed)
        self.assertEqual(security.hash_token(raw), hashed)

    def test_email_normalisation(self):
        self.assertEqual(security.normalise_email("  Ann@Example.COM "), "ann@example.com")
        self.assertTrue(security.is_valid_email("ann@example.com"))
        self.assertFalse(security.is_valid_email("not-an-email"))


class TransactionTests(AppTestCase):
    def test_a_failed_transaction_rolls_back(self):
        room, user = self.create_room(), self.create_user()

        def boom(conn):
            conn.execute(
                "INSERT INTO bookings (id, room_id, user_id, title, start_at, end_at,"
                " status, level_at_booking, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    models.new_id(), room.id, user.id, "會議",
                    taipei_at(1, 9), taipei_at(1, 10), models.CONFIRMED, 1,
                    taipei_at(0, 0), taipei_at(0, 0),
                ),
            )
            raise RuntimeError("deliberate failure")

        with self.assertRaises(RuntimeError):
            self.db.run_in_transaction(boom)

        self.assertEqual(self.query_all("SELECT id FROM bookings"), [])

    def test_concurrent_writers_are_serialised(self):
        # The preemption engine depends on this: two transactions that read
        # the same rows must not both commit.
        room, user = self.create_room(), self.create_user()
        barrier = threading.Barrier(2)
        winners: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            def work(conn):
                existing = conn.query_all(
                    "SELECT id FROM bookings WHERE room_id = ? AND status = ?",
                    (room.id, models.CONFIRMED),
                )
                if existing:
                    return None
                booking_id = models.new_id()
                conn.execute(
                    "INSERT INTO bookings (id, room_id, user_id, title, start_at,"
                    " end_at, status, level_at_booking, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        booking_id, room.id, user.id, "會議",
                        taipei_at(1, 9), taipei_at(1, 10), models.CONFIRMED, 1,
                        taipei_at(0, 0), taipei_at(0, 0),
                    ),
                )
                return booking_id

            # Sync before opening the transaction: BEGIN IMMEDIATE blocks the
            # loser until the winner commits, so a barrier inside the
            # transaction could never be reached by both threads.
            barrier.wait(timeout=10)
            result = self.db.run_in_transaction(work)
            if result:
                with lock:
                    winners.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(winners), 1, "exactly one writer should win")
        self.assertEqual(len(self.query_all("SELECT id FROM bookings")), 1)
