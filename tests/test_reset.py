"""Wiping the site's data (owner-operated reset).

The interesting failure mode here is not "the delete did not run" -- it is
"the delete ran over something it was supposed to keep", or "a table nobody
remembered was left full of the old data". Both are invisible in a test that
hand-checks two or three tables, so the core of this module is a scanner:
it fills *every* table, runs a scope, and then checks the kept/wiped split
across the whole table list. A twelfth table added later without a decision
about which scopes it belongs to fails
:meth:`TableCoverageTests.test_every_table_in_the_database_is_classified`
rather than quietly surviving every reset.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import shutil
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import manage
from app.config import Config
from app.db import POSTGRES
from app.db.migrations import MIGRATIONS
from app.models import new_id
from app.services import accounts, audit, provisioning, reset
from app.settings import DEFAULTS
from app.timeutil import now_utc
from tests.support import AppTestCase, taipei_at

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "reset.yml"

ADMIN_EMAIL = "owner@example.com"
ADMIN_PASSWORD = "the initial password"

#: The settings table is keyed by name, not by a uuid ``id``.
_PRIMARY_KEY = {"settings": "key"}

#: A settings row that ``seed_defaults`` will never put back, so the scanner
#: can tell "the settings table was emptied and re-seeded" apart from "the
#: settings table was left alone".
_SENTINEL_SETTING = "reset_test_sentinel"

#: Keys a wipe is *supposed* to bring straight back. ``settings`` is keyed by
#: name, so re-seeding recreates exactly the keys that were just deleted and a
#: plain before/after key comparison cannot tell the two apart -- which is the
#: whole reason for the sentinel above. Every other re-seeded table (the admin
#: account, the example rooms) gets fresh uuids, so it needs no exemption.
_RESEEDED_KEYS = {"settings": frozenset(DEFAULTS)}


def _config() -> Config:
    return Config(admin_email=ADMIN_EMAIL, admin_initial_password=ADMIN_PASSWORD)


class ResetTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.populate()

    # --- fixtures ---------------------------------------------------------

    def populate(self) -> None:
        """Put at least one row in every table the reset knows about."""
        self.admin = self.create_user(
            email="existing-admin@example.com", is_admin=True, level=10
        )
        self.member = self.create_user(email="member@example.com", level=3)
        self.room = self.create_room(name="會議室 A")
        start = taipei_at(3, 10)
        self.victim_booking = self.create_booking(
            room=self.room, user=self.member, start_at=start,
            end_at=start + timedelta(hours=1),
        )
        self.winner_booking = self.create_booking(
            room=self.room, user=self.admin, start_at=start + timedelta(days=1),
            end_at=start + timedelta(days=1, hours=1),
        )
        now = now_utc()

        def insert(conn) -> None:
            conn.execute(
                "INSERT INTO email_tokens (id, user_id, email, type, token_hash,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), self.member.id, self.member.email, "password_reset",
                 "hash-" + new_id(), now + timedelta(hours=1), now),
            )
            conn.execute(
                "INSERT INTO preemption_log (id, victim_booking_id,"
                " winner_booking_id, victim_user_id, winner_user_id, victim_level,"
                " winner_level, room_id, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), self.victim_booking.id, self.winner_booking.id,
                 self.member.id, self.admin.id, 3, 10, self.room.id, now),
            )
            conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " related_booking_id, attempts, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), self.member.email, "E5", "您的預約已被取消", "sent",
                 self.victim_booking.id, 1, now),
            )
            conn.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (new_id(), self.member.id, now, now + timedelta(days=7)),
            )
            conn.execute(
                "INSERT INTO room_closures (id, room_id, from_date, to_date,"
                " start_minutes, end_minutes, weekday_mask, reason, created_by,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), self.room.id, "2026-01-01", "2026-12-31",
                 8 * 60, 10 * 60, 0b1111111, "每日清潔", self.admin.id, now),
            )
            conn.execute(
                "INSERT INTO login_attempts (id, email, succeeded, created_at)"
                " VALUES (?, ?, ?, ?)",
                (new_id(), self.member.email, True, now),
            )
            conn.execute(
                "INSERT INTO cron_runs (id, job, started_at, finished_at, ok)"
                " VALUES (?, ?, ?, ?, ?)",
                (new_id(), "send_reminders", now, now, True),
            )
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (_SENTINEL_SETTING, json.dumps("present"), now),
            )
            # settings.updated_by is a foreign key into users, and the members
            # scope keeps the settings while deleting every user. Without a
            # row like this the reset would appear to work and then fail the
            # first time a real admin had edited a setting.
            conn.execute(
                "UPDATE settings SET updated_by = ? WHERE key = ?",
                (self.admin.id, "slot_minutes"),
            )
            audit.record(
                conn, actor_id=self.admin.id, action="level_changed",
                target_type="user", target_id=self.member.id,
            )

        self.db.run_in_transaction(insert)

    # --- inspection -------------------------------------------------------

    def live_tables(self) -> set[str]:
        """Every table the database actually has, straight from the catalogue."""
        if self.db.dialect == POSTGRES:
            rows = self.query_all(
                "SELECT tablename AS name FROM pg_tables"
                " WHERE schemaname = current_schema()"
            )
        else:
            rows = self.query_all(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        return {row["name"] for row in rows}

    def keys_in(self, table: str) -> set[str]:
        column = _PRIMARY_KEY.get(table, "id")
        return {
            str(row[column])
            for row in self.query_all(f"SELECT {column} FROM {table}")
        }

    def snapshot(self) -> dict[str, set[str]]:
        return {table: self.keys_in(table) for table in reset.ALL_TABLES}

    def run_reset(self, scope: str) -> reset.ResetReport:
        return reset.reset(self.db, _config(), scope=scope)


class TableCoverageTests(ResetTestBase):
    def test_every_table_in_the_database_is_classified(self):
        """A new table must be given a scope, not silently skipped."""
        self.assertEqual(
            self.live_tables() - {"schema_migrations"},
            set(reset.ALL_TABLES),
            "app/services/reset.py and the schema disagree about which tables exist",
        )

    def test_the_fixture_really_fills_every_table(self):
        """Otherwise the scanner below would pass on empty tables."""
        for table in reset.ALL_TABLES:
            with self.subTest(table=table):
                self.assertTrue(self.keys_in(table), f"{table} was left empty")

    def test_the_scopes_are_nested_from_least_to_most_destructive(self):
        booking_scope = reset.SCOPE_TABLES[reset.SCOPE_BOOKINGS]
        members = reset.SCOPE_TABLES[reset.SCOPE_MEMBERS]
        everything = reset.SCOPE_TABLES[reset.SCOPE_ALL]
        self.assertLess(booking_scope, members)
        self.assertLess(members, everything)
        self.assertEqual(everything, set(reset.ALL_TABLES))
        self.assertEqual(reset.SCOPES, ("bookings", "members", "all"))


class ScopeScannerTests(ResetTestBase):
    """The kept/wiped split, checked over the whole table list at once."""

    def assert_split(self, scope: str) -> reset.ResetReport:
        before = self.snapshot()
        migrations_before = self.query_all(
            "SELECT version FROM schema_migrations ORDER BY version"
        )

        report = self.run_reset(scope)

        after = self.snapshot()
        wiped = reset.SCOPE_TABLES[scope]
        for table in reset.ALL_TABLES:
            with self.subTest(table=table, scope=scope):
                if table in wiped:
                    # Anything still here that re-seeding would not have put
                    # back is a row the wipe missed. For settings that is
                    # precisely the sentinel, which is what makes this a real
                    # check rather than one satisfied by re-seeding.
                    survivors = (
                        before[table] & after[table]
                    ) - _RESEEDED_KEYS.get(table, frozenset())
                    self.assertFalse(
                        survivors, f"{table} kept {len(survivors)} old row(s)"
                    )
                    self.assertEqual(
                        report.removed[table],
                        len(before[table]),
                        f"{table}'s reported count does not match what it held",
                    )
                else:
                    self.assertLessEqual(
                        before[table], after[table], f"{table} lost rows"
                    )
                    self.assertNotIn(table, report.removed)

        self.assertEqual(
            migrations_before,
            self.query_all("SELECT version FROM schema_migrations ORDER BY version"),
            "schema_migrations was touched; the schema is not being reset",
        )
        self.assertEqual(set(report.kept), set(reset.ALL_TABLES) - wiped)
        return report

    def test_bookings_scope(self):
        self.assert_split(reset.SCOPE_BOOKINGS)

    def test_members_scope(self):
        self.assert_split(reset.SCOPE_MEMBERS)

    def test_all_scope(self):
        self.assert_split(reset.SCOPE_ALL)


class WhatSurvivesTests(ResetTestBase):
    """The consequences the owner will actually notice."""

    def test_bookings_scope_leaves_everyone_logged_in(self):
        sessions_before = self.keys_in("sessions")
        self.run_reset(reset.SCOPE_BOOKINGS)
        self.assertEqual(self.keys_in("sessions"), sessions_before)

    def test_bookings_scope_keeps_invitation_and_reset_links_working(self):
        tokens_before = self.keys_in("email_tokens")
        self.run_reset(reset.SCOPE_BOOKINGS)
        self.assertEqual(self.keys_in("email_tokens"), tokens_before)

    def test_bookings_scope_keeps_a_changed_setting(self):
        self.set_setting("slot_minutes", 15)
        self.run_reset(reset.SCOPE_BOOKINGS)
        self.assertEqual(self.settings().slot_minutes, 15)

    def test_members_scope_keeps_the_rooms_and_a_changed_setting(self):
        self.set_setting("slot_minutes", 15)
        rooms_before = self.keys_in("rooms")
        self.run_reset(reset.SCOPE_MEMBERS)
        self.assertEqual(self.keys_in("rooms"), rooms_before)
        self.assertEqual(self.settings().slot_minutes, 15)

    def test_members_scope_logs_everyone_out(self):
        self.run_reset(reset.SCOPE_MEMBERS)
        self.assertEqual(self.keys_in("sessions"), set())

    def test_members_scope_removes_administrators_too(self):
        self.run_reset(reset.SCOPE_MEMBERS)
        remaining = self.query_all("SELECT email FROM users")
        self.assertEqual([row["email"] for row in remaining], [ADMIN_EMAIL])

    def test_all_scope_puts_the_default_settings_back(self):
        self.set_setting("slot_minutes", 15)
        self.run_reset(reset.SCOPE_ALL)
        self.assertEqual(
            self.settings().slot_minutes, DEFAULTS["slot_minutes"]
        )
        self.assertNotIn(_SENTINEL_SETTING, self.keys_in("settings"))

    def test_all_scope_puts_the_example_rooms_back(self):
        self.run_reset(reset.SCOPE_ALL)
        names = {row["name"] for row in self.query_all("SELECT name FROM rooms")}
        self.assertEqual(
            names, {room["name"] for room in provisioning.EXAMPLE_ROOMS}
        )

    def test_members_scope_clears_the_setting_editor_it_is_deleting(self):
        """settings.updated_by is a foreign key into the users being wiped."""
        self.run_reset(reset.SCOPE_MEMBERS)
        dangling = self.query_all(
            "SELECT key FROM settings WHERE updated_by IS NOT NULL"
        )
        self.assertEqual(dangling, [])


class AdministratorTests(ResetTestBase):
    """After the wider two scopes the owner must still be able to get in."""

    def assert_admin_can_log_in(self) -> None:
        user = accounts.authenticate(self.db, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.assertTrue(user.is_admin)
        self.assertTrue(
            user.must_change_password,
            "the seeded password is sitting in a GitHub secret (spec §10.3)",
        )

    def test_members_scope_recreates_a_working_administrator(self):
        self.run_reset(reset.SCOPE_MEMBERS)
        self.assert_admin_can_log_in()

    def test_all_scope_recreates_a_working_administrator(self):
        self.run_reset(reset.SCOPE_ALL)
        self.assert_admin_can_log_in()

    def test_the_old_administrator_password_stops_working(self):
        self.run_reset(reset.SCOPE_ALL)
        with self.assertRaises(Exception):
            accounts.authenticate(
                self.db, "existing-admin@example.com", "correct horse battery"
            )


class AuditTrailTests(ResetTestBase):
    """The reset must leave a trace even though it wipes the trail itself."""

    def test_it_records_itself_after_the_wipe_so_the_entry_survives(self):
        self.run_reset(reset.SCOPE_ALL)
        actions = [row["action"] for row in self.query_all("SELECT action FROM audit_log")]
        self.assertIn(reset.DATA_RESET, actions)

    def test_the_entry_names_the_scope_and_the_counts(self):
        report = self.run_reset(reset.SCOPE_BOOKINGS)
        row = self.query_one(
            "SELECT detail FROM audit_log WHERE action = ?", (reset.DATA_RESET,)
        )
        detail = json.loads(row["detail"])
        self.assertEqual(detail["scope"], reset.SCOPE_BOOKINGS)
        self.assertEqual(detail["removed"], report.removed)

    def test_the_old_trail_is_gone(self):
        self.run_reset(reset.SCOPE_BOOKINGS)
        actions = [row["action"] for row in self.query_all("SELECT action FROM audit_log")]
        self.assertNotIn("level_changed", actions)


class AtomicityTests(ResetTestBase):
    """A half-finished wipe is worse than no wipe at all."""

    def test_a_failure_leaves_every_row_in_place(self):
        before = self.snapshot()

        # audit.record is the last statement of the wipe transaction, so
        # failing it means every DELETE has already run. If the transaction
        # is not really atomic, this is where it shows.
        with mock.patch.object(
            audit, "record", side_effect=RuntimeError("boom")
        ), self.assertRaises(RuntimeError):
            self.run_reset(reset.SCOPE_ALL)

        self.assertEqual(self.snapshot(), before)

    def test_an_unknown_scope_is_refused_before_anything_runs(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            reset.reset(self.db, _config(), scope="everything")
        self.assertEqual(self.snapshot(), before)


class CommandLineTests(unittest.TestCase):
    """`manage.py reset` end to end, against a database it builds itself.

    Deliberately not an :class:`AppTestCase`: the point is to exercise the
    path the workflow takes -- secrets in the environment, a DATABASE_URL,
    argv -- rather than a database handed over by the harness.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="reset-cli-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        path = os.path.join(self.tmpdir, "cli.sqlite3")

        env = {
            "DATABASE_URL": f"sqlite://{path}",
            "EMAIL_PROVIDER_API_KEY": "x",
            "EMAIL_FROM_ADDRESS": "noreply@example.com",
            "APP_BASE_URL": "https://example.onrender.com",
            "SESSION_SECRET": "s" * 32,
            "ADMIN_EMAIL": ADMIN_EMAIL,
            "ADMIN_INITIAL_PASSWORD": ADMIN_PASSWORD,
            "CRON_SECRET": "c" * 32,
            "EMAIL_TRANSPORT": "fake",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.run_cli(["migrate"])
        from app.db import create_database

        self.db = create_database(f"sqlite://{path}")
        self.addCleanup(self.db.close)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manage.main(argv)
        return code, stdout.getvalue() + stderr.getvalue()

    def room_count(self) -> int:
        return self.db.run_in_transaction(
            lambda conn: conn.query_value("SELECT COUNT(*) FROM rooms")
        )

    def test_the_parser_offers_exactly_the_service_scopes(self):
        """manage.py spells the scopes out; they must not drift from the service."""
        parser = manage.build_parser()
        for scope in reset.SCOPES:
            with self.subTest(scope=scope):
                args = parser.parse_args(["reset", "--scope", scope])
                self.assertEqual(args.scope, scope)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["reset", "--scope", "everything"])

    def test_it_refuses_without_a_confirmation(self):
        code, output = self.run_cli(["reset", "--scope", "all"])
        self.assertEqual(code, 1)
        self.assertIn("DELETE", output)
        self.assertEqual(self.room_count(), 3, "data was touched anyway")

    def test_it_refuses_a_lowercase_confirmation(self):
        code, _ = self.run_cli(["reset", "--scope", "all", "--confirm", "delete"])
        self.assertEqual(code, 1)
        self.assertEqual(self.room_count(), 3, "data was touched anyway")

    def test_it_refuses_the_scope_name_as_a_confirmation(self):
        code, _ = self.run_cli(["reset", "--scope", "all", "--confirm", "all"])
        self.assertEqual(code, 1)
        self.assertEqual(self.room_count(), 3, "data was touched anyway")

    def test_it_names_a_missing_secret_rather_than_half_resetting(self):
        with mock.patch.dict(os.environ, {"ADMIN_INITIAL_PASSWORD": ""}):
            code, output = self.run_cli(
                ["reset", "--scope", "all", "--confirm", "DELETE"]
            )
        self.assertEqual(code, 1)
        self.assertIn("ADMIN_INITIAL_PASSWORD", output)
        self.assertEqual(self.room_count(), 3, "data was touched anyway")

    def test_it_prints_the_plan_and_then_the_per_table_counts(self):
        code, output = self.run_cli(
            ["reset", "--scope", "members", "--confirm", "DELETE"]
        )
        self.assertEqual(code, 0)
        self.assertIn("cannot be undone", output)
        for table in reset.SCOPE_TABLES["members"]:
            with self.subTest(table=table):
                self.assertIn(table, output)
        # The seeded admin is the one account it had, and it must be reported.
        self.assertRegex(output, r"users\s+1")
        self.assertEqual(self.room_count(), 3, "members scope removed the rooms")

    def test_the_all_scope_leaves_a_freshly_deployed_system(self):
        code, _ = self.run_cli(["reset", "--scope", "all", "--confirm", "DELETE"])
        self.assertEqual(code, 0)
        self.assertEqual(self.room_count(), 3)
        user = accounts.authenticate(self.db, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.assertTrue(user.must_change_password)

    def test_a_second_migrate_after_a_full_reset_is_still_a_no_op(self):
        """The schema survived, so migrate must not try to re-create it."""
        self.run_cli(["reset", "--scope", "all", "--confirm", "DELETE"])
        code, _ = self.run_cli(["migrate"])
        self.assertEqual(code, 0)
        applied = self.db.run_in_transaction(
            lambda conn: conn.query_all("SELECT version FROM schema_migrations")
        )
        self.assertEqual(len(applied), len(MIGRATIONS))


class WorkflowTests(unittest.TestCase):
    """The button itself. Checked as text -- PyYAML is not in the stdlib."""

    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.triggers = self.text.split("jobs:", 1)[0]

    def test_it_exists_and_is_a_manual_button(self):
        self.assertIn("workflow_dispatch:", self.triggers)

    def test_it_can_never_fire_on_its_own(self):
        # A push or a schedule trigger on this workflow would delete the
        # site's data without anybody asking.
        self.assertNotRegex(self.triggers, r"^\s*push:", )
        self.assertNotRegex(self.triggers, r"^\s*schedule:", )
        self.assertNotRegex(self.triggers, r"^\s*pull_request:", )

    def test_it_asks_for_a_scope_and_a_confirmation(self):
        self.assertIn("scope:", self.text)
        self.assertIn("confirm:", self.text)
        # Both are required, so a bare "Run workflow" click cannot submit.
        self.assertEqual(self.text.count("required: true"), 2)

    def test_the_scope_choices_match_the_service(self):
        for scope in reset.SCOPES:
            with self.subTest(scope=scope):
                self.assertIn(f"- {scope}", self.text)

    def test_the_least_destructive_scope_is_the_default(self):
        self.assertIn(f"default: {reset.SCOPES[0]}", self.text)

    def test_a_wrong_confirmation_aborts_before_anything_else_runs(self):
        guard = self.text.index('"$CONFIRM" != "DELETE"')
        self.assertLess(guard, self.text.index("actions/checkout"))
        self.assertLess(guard, self.text.index("manage.py reset"))

    def test_the_free_text_input_is_never_interpolated_into_the_shell(self):
        # ${{ inputs.confirm }} written inside a run: block would let whatever
        # was typed into the box run as a shell command. Reaching it only
        # through env: makes that impossible, so every mention must be one.
        mentions = [
            line.strip() for line in self.text.splitlines() if "inputs." in line
        ]
        self.assertTrue(mentions)
        for line in mentions:
            with self.subTest(line=line):
                self.assertRegex(line, r"^(CONFIRM|SCOPE): \$\{\{ inputs\.(confirm|scope) \}\}$")

    def test_it_names_the_admin_secrets_it_needs_to_rebuild_the_owner(self):
        for secret in ("ADMIN_EMAIL", "ADMIN_INITIAL_PASSWORD"):
            with self.subTest(secret=secret):
                self.assertIn(f"secrets.{secret}", self.text)
        self.assertIn("check-secrets", self.text)

    def test_it_reports_what_was_destroyed(self):
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)
        self.assertIn("reset-output.txt", self.text)


class DocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / "ROLLBACK.md").read_text(encoding="utf-8")

    def test_it_documents_every_scope(self):
        for scope in reset.SCOPES:
            with self.subTest(scope=scope):
                self.assertIn(scope, self.text)

    def test_it_says_the_reset_cannot_be_undone(self):
        self.assertIn("無法復原", self.text)

    def test_it_explains_how_to_log_in_again(self):
        self.assertIn("ADMIN_INITIAL_PASSWORD", self.text)
