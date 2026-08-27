"""Deleting an account without deleting the history that names it.

The requirement has two halves that pull against each other: a member must be
able to remove themselves, and every record that mentions them -- bookings,
preemptions, the audit trail, the email log -- must survive. A DELETE does
neither cleanly: it would either cascade the history away or fail on the
foreign keys pointing at the row.

So deletion is anonymisation. These tests assert both halves: that nothing
identifying is left behind, and that nothing historical went with it.
"""

from __future__ import annotations

from datetime import timedelta

from app import models
from app.errors import AuthError, ConflictError, ForbiddenError, NotFoundError
from app.services import accounts, sessions
from app.timeutil import now_utc
from tests.support import AppTestCase, taipei_at

_PASSWORD = "a decent passphrase"


class DeletionTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.room = self.create_room(name="Meeting Room 1")
        self.admin = self.create_user(
            email="admin@example.com",
            password=_PASSWORD,
            is_admin=True,
            full_name="The Administrator",
        )
        self.member = self.create_user(
            email="member@example.com",
            password=_PASSWORD,
            full_name="Christopher Vandenberg",
            department="Operations",
            phone="0912345678",
        )

    def user_row(self, user_id: str) -> dict:
        return self.query_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def book(self, *, days_ahead: int = 1, hour: int = 10, user=None):
        start = taipei_at(days_ahead, hour)
        return self.create_booking(
            room=self.room,
            user=user or self.member,
            start_at=start,
            end_at=start + timedelta(hours=1),
            title="Quarterly planning review",
        )


class AnonymisationTests(DeletionTestBase):
    def test_nothing_identifying_survives_on_the_row(self):
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        row = self.user_row(self.member.id)

        blob = " ".join(str(v) for v in row.values())
        for secret in (
            "member@example.com",
            "Christopher Vandenberg",
            "Operations",
            "0912345678",
        ):
            self.assertNotIn(secret, blob, f"{secret!r} is still on the row")

    def test_the_row_itself_is_kept_so_history_still_resolves(self):
        booking = self.book()
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)

        self.assertIsNotNone(self.user_row(self.member.id), "the row was removed")
        still_there = self.query_one(
            "SELECT user_id FROM bookings WHERE id = ?", (booking.id,)
        )
        self.assertEqual(still_there["user_id"], self.member.id)

    def test_the_tombstone_is_marked_as_deleted(self):
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        row = self.user_row(self.member.id)
        self.assertIsNotNone(row["deleted_at"])
        self.assertFalse(models.User.from_row(row).is_active)
        self.assertFalse(models.User.from_row(row).can_book)

    def test_the_address_is_released_for_re_registration(self):
        """Deleting must not lock the person out of ever signing up again."""
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)

        result = accounts.register(
            self.db,
            email="member@example.com",
            password=_PASSWORD,
            full_name="Christopher Vandenberg",
            department="Operations",
            phone="0912345678",
        )
        self.assertTrue(result.emails, "registering the freed address sent nothing")
        rows = self.query_all(
            "SELECT id FROM users WHERE email = ?", ("member@example.com",)
        )
        self.assertEqual(len(rows), 1, "the freed address is not unique again")

    def test_an_admin_loses_the_admin_flag(self):
        second = self.create_user(
            email="admin2@example.com", password=_PASSWORD, is_admin=True
        )
        accounts.delete_account(self.db, actor=self.admin, user_id=second.id)
        self.assertFalse(bool(self.user_row(second.id)["is_admin"]))


class NoWayBackTests(DeletionTestBase):
    def test_the_old_password_no_longer_works(self):
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        with self.assertRaises(Exception):
            accounts.authenticate(self.db, "member@example.com", _PASSWORD)

    def test_every_live_session_is_revoked(self):
        cookie, _ = sessions.create_session(self.db, self.member)
        self.assertIsNotNone(sessions.resolve_session(self.db, cookie))

        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        self.assertIsNone(
            sessions.resolve_session(self.db, cookie),
            "a session outlived the account it belonged to",
        )

    def test_an_outstanding_reset_link_cannot_resurrect_the_account(self):
        accounts.request_password_reset(self.db, "member@example.com")
        token = self.query_one(
            "SELECT id FROM email_tokens WHERE user_id = ? AND type = ?",
            (self.member.id, "password_reset"),
        )
        self.assertIsNotNone(token, "no reset token was issued to set up the test")

        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        row = self.query_one(
            "SELECT revoked_at FROM email_tokens WHERE id = ?", (token["id"],)
        )
        self.assertIsNotNone(row["revoked_at"], "the reset link is still live")

    def test_deleting_the_same_account_twice_is_refused(self):
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        with self.assertRaises(NotFoundError):
            accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)


class BookingsAreReleasedTests(DeletionTestBase):
    def test_future_bookings_are_cancelled_so_the_room_is_usable(self):
        """Nobody could release them otherwise -- the owner cannot log in."""
        booking = self.book()
        result = accounts.delete_account(
            self.db, actor=self.admin, user_id=self.member.id
        )

        self.assertEqual(result.cancelled_bookings, 1)
        row = self.query_one(
            "SELECT status, cancelled_at FROM bookings WHERE id = ?", (booking.id,)
        )
        self.assertEqual(row["status"], models.CANCELLED_BY_ADMIN)
        self.assertIsNotNone(row["cancelled_at"])

    def test_deleting_yourself_records_it_as_your_own_cancellation(self):
        booking = self.book()
        accounts.delete_account(
            self.db,
            actor=self.member,
            user_id=self.member.id,
            current_password=_PASSWORD,
        )
        row = self.query_one("SELECT status FROM bookings WHERE id = ?", (booking.id,))
        self.assertEqual(row["status"], models.CANCELLED_BY_USER)

    def test_past_bookings_are_left_exactly_as_they_were(self):
        start = now_utc() - timedelta(days=2)
        past = self.create_booking(
            room=self.room,
            user=self.member,
            start_at=start,
            end_at=start + timedelta(hours=1),
            title="An earlier meeting",
        )
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        row = self.query_one(
            "SELECT status, title FROM bookings WHERE id = ?", (past.id,)
        )
        self.assertEqual(row["status"], models.CONFIRMED, "history was rewritten")
        self.assertEqual(row["title"], "An earlier meeting")

    def test_the_pending_reminder_for_a_cancelled_booking_is_dropped(self):
        booking = self.book()
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "INSERT INTO email_log (id, type, to_email, subject, status,"
                " attempts, dedupe_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    models.new_id(),
                    "E10",
                    "member@example.com",
                    "Reminder",
                    "queued",
                    0,
                    f"reminder:{booking.id}",
                    now_utc(),
                ),
            )
        )
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        row = self.query_one(
            "SELECT status FROM email_log WHERE dedupe_key = ?",
            (f"reminder:{booking.id}",),
        )
        self.assertEqual(row["status"], "skipped")


class PermissionTests(DeletionTestBase):
    def test_a_member_cannot_delete_someone_else(self):
        other = self.create_user(email="other@example.com", password=_PASSWORD)
        with self.assertRaises(ForbiddenError):
            accounts.delete_account(
                self.db,
                actor=self.member,
                user_id=other.id,
                current_password=_PASSWORD,
            )
        self.assertIsNone(self.user_row(other.id)["deleted_at"])

    def test_deleting_yourself_needs_the_current_password(self):
        with self.assertRaises(AuthError):
            accounts.delete_account(
                self.db,
                actor=self.member,
                user_id=self.member.id,
                current_password="not the password",
            )
        with self.assertRaises(AuthError):
            accounts.delete_account(
                self.db, actor=self.member, user_id=self.member.id
            )
        self.assertIsNone(self.user_row(self.member.id)["deleted_at"])

    def test_an_admin_does_not_need_the_members_password(self):
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        self.assertIsNotNone(self.user_row(self.member.id)["deleted_at"])

    def test_the_last_administrator_cannot_be_deleted(self):
        """An installation with no admin cannot approve anyone or undo this."""
        with self.assertRaises(ConflictError):
            accounts.delete_account(
                self.db,
                actor=self.admin,
                user_id=self.admin.id,
                current_password=_PASSWORD,
            )
        self.assertIsNone(self.user_row(self.admin.id)["deleted_at"])

    def test_an_admin_may_go_once_another_one_remains(self):
        self.create_user(email="admin2@example.com", password=_PASSWORD, is_admin=True)
        accounts.delete_account(
            self.db,
            actor=self.admin,
            user_id=self.admin.id,
            current_password=_PASSWORD,
        )
        self.assertIsNotNone(self.user_row(self.admin.id)["deleted_at"])

    def test_a_suspended_admin_does_not_count_as_the_one_remaining(self):
        spare = self.create_user(
            email="admin2@example.com", password=_PASSWORD, is_admin=True
        )
        accounts.set_suspended(
            self.db, actor=self.admin, user_id=spare.id, suspended=True
        )
        with self.assertRaises(ConflictError):
            accounts.delete_account(
                self.db,
                actor=self.admin,
                user_id=self.admin.id,
                current_password=_PASSWORD,
            )


class TrailTests(DeletionTestBase):
    def test_the_deletion_is_audited(self):
        self.book()
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        rows = self.query_all(
            "SELECT actor_user_id, target_id, detail FROM audit_log"
            " WHERE action = ?",
            ("user_deleted",),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_user_id"], self.admin.id)
        self.assertEqual(rows[0]["target_id"], self.member.id)
        self.assertIn("bookings_cancelled", rows[0]["detail"])

    def test_the_email_log_keeps_its_rows_but_not_the_address(self):
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "INSERT INTO email_log (id, type, to_email, subject, status,"
                " attempts, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    models.new_id(),
                    "E4",
                    "member@example.com",
                    "Booking confirmed",
                    "sent",
                    1,
                    now_utc(),
                ),
            )
        )
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)

        rows = self.query_all("SELECT to_email, type FROM email_log")
        self.assertTrue(rows, "the delivery log was emptied")
        self.assertNotIn(
            "member@example.com",
            " ".join(r["to_email"] for r in rows),
            "the deleted address is still in the outbound log",
        )

    def test_a_preemption_record_naming_them_is_untouched(self):
        victim_booking = self.book()
        winner_booking = self.book(days_ahead=2, user=self.admin)
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "INSERT INTO preemption_log (id, victim_booking_id,"
                " winner_booking_id, victim_user_id, winner_user_id,"
                " victim_level, winner_level, room_id, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    models.new_id(),
                    victim_booking.id,
                    winner_booking.id,
                    self.member.id,
                    self.admin.id,
                    1,
                    5,
                    self.room.id,
                    now_utc(),
                ),
            )
        )
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)

        rows = self.query_all(
            "SELECT victim_user_id FROM preemption_log WHERE victim_user_id = ?",
            (self.member.id,),
        )
        self.assertEqual(len(rows), 1, "the preemption record went with the account")


# --- the screens ------------------------------------------------------------


class WebFlowTestBase(DeletionTestBase):
    def setUp(self) -> None:
        super().setUp()
        from app.config import Config
        from app.web.app import create_app
        from tests.webclient import Client

        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)

    def login(self, email: str) -> None:
        self.client.get("/login")
        self.client.post("/login", form={"email": email, "password": _PASSWORD})


class MemberScreenTests(WebFlowTestBase):
    def test_the_account_page_is_reachable_from_the_navigation(self):
        """It is also the only route to the password screen."""
        self.login("member@example.com")
        day = self.client.get("/day")
        self.assertIn('href="/account"', day.text)

        account = self.client.get("/account")
        self.assertEqual(account.status, 200)
        self.assertIn('href="/password"', account.text)

    def test_deleting_yourself_signs_you_out_and_says_so(self):
        self.login("member@example.com")
        response = self.client.post(
            "/account/delete", form={"current_password": _PASSWORD}
        )
        self.assertEqual(response.status, 303)
        self.assertIn("deleted=1", dict(response.headers)["Location"])

        self.assertIsNotNone(self.user_row(self.member.id)["deleted_at"])
        landing = self.client.get("/login?deleted=1")
        self.assertIn("deleted", landing.text.lower())

        # And the session really is gone, not merely redirected away from.
        self.assertEqual(self.client.get("/my").status, 303)

    def test_the_wrong_password_deletes_nothing_and_explains_itself(self):
        self.login("member@example.com")
        response = self.client.post(
            "/account/delete", form={"current_password": "not it"}
        )
        self.assertNotEqual(response.status, 303, "it deleted on a wrong password")
        self.assertIsNone(self.user_row(self.member.id)["deleted_at"])
        self.assertEqual(self.client.get("/my").status, 200, "still signed in")

    def test_a_member_cannot_reach_the_admin_delete_route(self):
        other = self.create_user(email="other@example.com", password=_PASSWORD)
        self.login("member@example.com")
        response = self.client.post(f"/admin/members/{other.id}/delete", form={})
        self.assertIn(response.status, (303, 403), f"got {response.status}")
        self.assertIsNone(self.user_row(other.id)["deleted_at"])


class AdminScreenTests(WebFlowTestBase):
    def test_the_row_offers_a_delete_action(self):
        self.login("admin@example.com")
        page = self.client.get("/admin/members")
        self.assertIn(f"/admin/members/{self.member.id}/delete", page.text)

    def test_it_asks_before_doing_it(self):
        self.login("admin@example.com")
        confirm = self.client.get(f"/admin/members/{self.member.id}/delete")

        self.assertEqual(confirm.status, 200)
        self.assertIn("Christopher Vandenberg", confirm.text)
        self.assertIn("member@example.com", confirm.text)
        self.assertIsNone(
            self.user_row(self.member.id)["deleted_at"],
            "merely opening the confirmation deleted the account",
        )

    def test_confirming_deletes_and_reports_back(self):
        self.login("admin@example.com")
        self.client.get(f"/admin/members/{self.member.id}/delete")
        response = self.client.post(
            f"/admin/members/{self.member.id}/delete", form={}
        )

        self.assertEqual(response.status, 303)
        self.assertIn("msg=account_deleted", dict(response.headers)["Location"])
        self.assertIsNotNone(self.user_row(self.member.id)["deleted_at"])

    def test_the_deleted_member_is_still_listed_but_has_no_actions_left(self):
        self.login("admin@example.com")
        accounts.delete_account(self.db, actor=self.admin, user_id=self.member.id)
        page = self.client.get("/admin/members")

        self.assertNotIn(
            f"/admin/members/{self.member.id}/level", page.text,
            "a tombstone still offers a level change",
        )
        self.assertNotIn(
            f"/admin/members/{self.member.id}/suspend", page.text,
            "a tombstone still offers suspension",
        )
        self.assertNotIn("Christopher Vandenberg", page.text)

    def test_an_admin_deleting_themselves_is_sent_to_the_member_screen(self):
        """Self-deletion costs your own password wherever you start it.

        The admin route has no password to offer, so before this it reported
        the credentials as wrong and left no way forward.
        """
        self.login("admin@example.com")
        for response in (
            self.client.get(f"/admin/members/{self.admin.id}/delete"),
            self.client.post(f"/admin/members/{self.admin.id}/delete", form={}),
        ):
            self.assertEqual(response.status, 303)
            self.assertEqual(dict(response.headers)["Location"], "/account")
        self.assertIsNone(self.user_row(self.admin.id)["deleted_at"])

        row = self.client.get("/admin/members")
        self.assertNotIn(f"/admin/members/{self.admin.id}/delete", row.text)

    def test_deleting_the_only_admin_is_refused_with_a_reason(self):
        self.login("admin@example.com")
        response = self.client.post(
            "/account/delete", form={"current_password": _PASSWORD}
        )
        self.assertNotEqual(response.status, 303, "the last admin was deleted")
        self.assertIsNone(self.user_row(self.admin.id)["deleted_at"])
        self.assertNotIn("error.LAST_ADMIN", response.text, "the error has no message")
        self.assertIn("administrator", response.text.lower())
