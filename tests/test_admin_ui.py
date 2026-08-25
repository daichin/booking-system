"""Tests for the admin console (Task 6, spec §6.6, §8, §6.7, §9.4, §10.4)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app import models
from app.config import Config
from app.errors import AppError
from app.i18n import t
from app.services import accounts, mailer, sessions
from app.web.app import create_app
from tests.support import AppTestCase, taipei_at
from tests.webclient import Client


def _token_from_context(context: dict) -> str:
    """Pull the raw one-time token out of an emailed link (see
    tests/test_accounts.py for the original of this helper)."""
    for key in ("verify_url", "invite_url", "reset_url"):
        url = context.get(key)
        if url:
            return parse_qs(urlparse(url).query)["token"][0]
    raise KeyError(f"no token-bearing url in context: {context!r}")


#: Every admin route, with dummy path parameters. Used to sweep the
#: non-admin/anonymous access-control tests.
_ADMIN_ROUTES = [
    ("GET", "/admin"),
    ("GET", "/admin/approvals"),
    ("POST", "/admin/approvals/dummy/approve"),
    ("POST", "/admin/approvals/dummy/reject"),
    ("GET", "/admin/members"),
    ("POST", "/admin/members/dummy/level"),
    ("POST", "/admin/members/dummy/suspend"),
    ("POST", "/admin/members/dummy/reactivate"),
    ("GET", "/admin/invitations"),
    ("POST", "/admin/invitations"),
    ("POST", "/admin/invitations/dummy/revoke"),
    ("GET", "/admin/rooms"),
    ("POST", "/admin/rooms"),
    ("POST", "/admin/rooms/dummy"),
    ("POST", "/admin/rooms/dummy/deactivate"),
    ("POST", "/admin/rooms/dummy/activate"),
    ("GET", "/admin/bookings"),
    ("POST", "/admin/bookings/dummy/cancel"),
    ("GET", "/admin/preemptions"),
    ("GET", "/admin/settings"),
    ("POST", "/admin/settings"),
    ("GET", "/admin/emails"),
    ("GET", "/admin/audit"),
]


class AdminUITestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = Config(
            base_url="http://testserver",
            cron_secret="cron-secret-value",
            admin_email="owner@example.com",
            admin_initial_password="an initial password",
            email_transport="fake",
        )
        self.app = create_app(self.db, self.config)
        self.client = Client(self.app)
        # Primes the CSRF cookie so later POSTs (including anonymous ones in
        # the access-control sweep) can supply a matching token.
        self.client.get("/")

    def login(self, user: models.User) -> None:
        raw, _ = sessions.create_session(self.db, user)
        self.client.cookies["session"] = raw


# =============================================================================
# Access control
# =============================================================================


class AccessControlTests(AdminUITestBase):
    def test_non_admin_gets_403_on_every_admin_route(self):
        member = self.create_user(status=models.ACTIVE, is_admin=False)
        self.login(member)
        for method, path in _ADMIN_ROUTES:
            if method == "GET":
                response = self.client.get(path)
            else:
                response = self.client.post(path, form={})
            self.assertEqual(
                response.status, 403, f"{method} {path} -> {response.status}"
            )

    def test_anonymous_visitor_is_redirected_to_login(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status, 303)
        self.assertTrue(response.location.startswith("/login"))

        response2 = self.client.get("/admin/settings")
        self.assertEqual(response2.status, 303)
        self.assertTrue(response2.location.startswith("/login"))


# =============================================================================
# Approvals
# =============================================================================


class ApprovalsTests(AdminUITestBase):
    def test_pending_user_appears_and_approving_activates_and_sends_e2(self):
        admin = self.create_user(is_admin=True, status=models.ACTIVE)
        pending = self.create_user(status=models.PENDING_APPROVAL, full_name="王小明")
        self.login(admin)

        listing = self.client.get("/admin/approvals")
        self.assertEqual(listing.status, 200)
        self.assertIn("王小明", listing.text)

        response = self.client.post(f"/admin/approvals/{pending.id}/approve", form={})
        self.assertEqual(response.status, 303)

        self.assertEqual(self.get_user(pending.id).status, models.ACTIVE)
        rows = self.query_all(
            "SELECT * FROM email_log WHERE type = 'E2' AND to_email = ?",
            (pending.email,),
        )
        self.assertEqual(len(rows), 1)

    def test_rejecting_sets_status_and_sends_e3(self):
        admin = self.create_user(is_admin=True)
        pending = self.create_user(status=models.PENDING_APPROVAL)
        self.login(admin)

        response = self.client.post(f"/admin/approvals/{pending.id}/reject", form={})
        self.assertEqual(response.status, 303)
        self.assertEqual(self.get_user(pending.id).status, models.REJECTED)

        rows = self.query_all(
            "SELECT * FROM email_log WHERE type = 'E3' AND to_email = ?",
            (pending.email,),
        )
        self.assertEqual(len(rows), 1)


# =============================================================================
# Members
# =============================================================================


class MembersTests(AdminUITestBase):
    def test_level_change_is_audited_and_range_is_enforced(self):
        admin = self.create_user(is_admin=True)
        member = self.create_user(level=2)
        self.login(admin)

        ok = self.client.post(f"/admin/members/{member.id}/level", form={"level": "7"})
        self.assertEqual(ok.status, 303)
        self.assertEqual(self.get_user(member.id).level, 7)

        audit_rows = self.query_all(
            "SELECT * FROM audit_log WHERE action = 'level_changed' AND target_id = ?",
            (member.id,),
        )
        self.assertEqual(len(audit_rows), 1)

        bad = self.client.post(f"/admin/members/{member.id}/level", form={"level": "99"})
        self.assertEqual(bad.status, 303)
        self.assertIn("err=", bad.location)
        self.assertEqual(self.get_user(member.id).level, 7)  # unchanged

    def test_suspend_and_reactivate(self):
        admin = self.create_user(is_admin=True)
        member = self.create_user(status=models.ACTIVE)
        self.login(admin)

        suspended = self.client.post(f"/admin/members/{member.id}/suspend", form={})
        self.assertEqual(suspended.status, 303)
        self.assertEqual(self.get_user(member.id).status, models.SUSPENDED)

        reactivated = self.client.post(f"/admin/members/{member.id}/reactivate", form={})
        self.assertEqual(reactivated.status, 303)
        self.assertEqual(self.get_user(member.id).status, models.ACTIVE)

    def test_search_and_filter(self):
        admin = self.create_user(is_admin=True)
        self.create_user(full_name="張三", email="zhang@example.com", level=5)
        self.create_user(full_name="李四", email="li@example.com", level=1)
        self.login(admin)

        response = self.client.get("/admin/members?q=zhang")
        self.assertEqual(response.status, 200)
        self.assertIn("張三", response.text)
        self.assertNotIn("李四", response.text)


# =============================================================================
# Invitations
# =============================================================================


class InvitationsTests(AdminUITestBase):
    def test_sending_creates_a_token_and_sends_e8(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)

        response = self.client.post(
            "/admin/invitations",
            form={"emails": "invitee@example.com", "level": "3"},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("invitee@example.com", response.text)

        rows = self.query_all(
            "SELECT * FROM email_log WHERE type = 'E8' AND to_email = ?",
            ("invitee@example.com",),
        )
        self.assertEqual(len(rows), 1)

        listing = self.client.get("/admin/invitations")
        self.assertIn("invitee@example.com", listing.text)

    def test_revoking_makes_the_invite_link_unusable(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)

        # Call the service directly to recover the raw token -- it is only
        # ever emailed, never persisted (see app/services/accounts.py).
        results = accounts.invite(self.db, admin, ["revoke-me@example.com"], None)
        raw_token = _token_from_context(results[0].emails[0].context)
        token_id = results[0].token_id

        response = self.client.post(f"/admin/invitations/{token_id}/revoke", form={})
        self.assertEqual(response.status, 303)

        listing = self.client.get("/admin/invitations")
        self.assertNotIn("revoke-me@example.com", listing.text)

        with self.assertRaises(AppError) as ctx:
            accounts.accept_invitation(
                self.db,
                raw_token,
                password="a reasonably long password",
                full_name="X",
                department="Y",
                phone="123",
            )
        self.assertErrorCode(ctx, "TOKEN_INVALID")


# =============================================================================
# Rooms
# =============================================================================


class RoomsTests(AdminUITestBase):
    def test_create_and_edit_room(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)

        created = self.client.post(
            "/admin/rooms",
            form={
                "name": "會議室 X",
                "capacity": "8",
                "location": "5樓",
                "equipment_note": "白板",
                "open_time": "09:00",
                "close_time": "18:00",
            },
        )
        self.assertEqual(created.status, 303)
        room_row = self.query_one("SELECT * FROM rooms WHERE name = ?", ("會議室 X",))
        self.assertIsNotNone(room_row)

        edited = self.client.post(
            f"/admin/rooms/{room_row['id']}",
            form={
                "name": "會議室 X 改",
                "capacity": "10",
                "location": "5樓",
                "equipment_note": "白板",
                "open_time": "08:00",
                "close_time": "20:00",
            },
        )
        self.assertEqual(edited.status, 303)
        updated = self.get_room(room_row["id"])
        self.assertEqual(updated.name, "會議室 X 改")
        self.assertEqual(updated.capacity, 10)

    def test_deactivating_a_room_with_future_bookings_requires_confirmation(self):
        admin = self.create_user(is_admin=True)
        owner = self.create_user(status=models.ACTIVE)
        room = self.create_room()
        booking = self.create_booking(
            room=room, user=owner, start_at=taipei_at(3, 10, 0), end_at=taipei_at(3, 11, 0)
        )
        self.login(admin)

        first = self.client.post(f"/admin/rooms/{room.id}/deactivate", form={})
        self.assertEqual(first.status, 200)
        self.assertIn(t("admin.rooms.confirm_title"), first.text)
        self.assertEqual(self.get_booking(booking.id).status, models.CONFIRMED)
        self.assertTrue(self.get_room(room.id).is_active)

        second = self.client.post(
            f"/admin/rooms/{room.id}/deactivate", form={"confirm_cancel": "1"}
        )
        self.assertEqual(second.status, 303)
        self.assertFalse(self.get_room(room.id).is_active)
        self.assertEqual(self.get_booking(booking.id).status, models.CANCELLED_BY_ADMIN)

        rows = self.query_all(
            "SELECT * FROM email_log WHERE type = 'E5' AND to_email = ?", (owner.email,)
        )
        self.assertEqual(len(rows), 1)

    def test_deactivating_a_room_with_no_future_bookings_needs_no_confirmation(self):
        admin = self.create_user(is_admin=True)
        room = self.create_room()
        self.login(admin)

        response = self.client.post(f"/admin/rooms/{room.id}/deactivate", form={})
        self.assertEqual(response.status, 303)
        self.assertFalse(self.get_room(room.id).is_active)


# =============================================================================
# Bookings
# =============================================================================


class BookingsTests(AdminUITestBase):
    def test_admin_cancels_someone_elses_booking_and_owner_gets_e5(self):
        admin = self.create_user(is_admin=True)
        owner = self.create_user(status=models.ACTIVE)
        room = self.create_room()
        booking = self.create_booking(
            room=room, user=owner, start_at=taipei_at(2, 14, 0), end_at=taipei_at(2, 15, 0)
        )
        self.login(admin)

        response = self.client.post(f"/admin/bookings/{booking.id}/cancel", form={})
        self.assertEqual(response.status, 303)
        self.assertEqual(self.get_booking(booking.id).status, models.CANCELLED_BY_ADMIN)

        rows = self.query_all(
            "SELECT * FROM email_log WHERE type = 'E5' AND to_email = ?", (owner.email,)
        )
        self.assertEqual(len(rows), 1)

    def test_csv_export_returns_csv_with_a_header_row(self):
        admin = self.create_user(is_admin=True)
        owner = self.create_user(status=models.ACTIVE)
        room = self.create_room()
        self.create_booking(
            room=room, user=owner, start_at=taipei_at(2, 9, 0), end_at=taipei_at(2, 10, 0)
        )
        self.login(admin)

        response = self.client.get("/admin/bookings?format=csv")
        self.assertEqual(response.status, 200)
        self.assertIn("text/csv", response.header("Content-Type"))
        self.assertIn("attachment", response.header("Content-Disposition"))
        body = response.body.decode("utf-8-sig")
        header = body.splitlines()[0]
        self.assertIn(t("admin.bookings.csv_room"), header)
        self.assertIn(t("admin.bookings.csv_owner_email"), header)


# =============================================================================
# Preemption log and audit trail CSV export
# =============================================================================


class PreemptionsAndAuditTests(AdminUITestBase):
    def test_preemption_log_csv_export(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)
        response = self.client.get("/admin/preemptions?format=csv")
        self.assertEqual(response.status, 200)
        self.assertIn("text/csv", response.header("Content-Type"))
        header = response.body.decode("utf-8-sig").splitlines()[0]
        self.assertIn(t("admin.preemptions.csv_room"), header)

    def test_audit_trail_shows_level_changes_and_exports_csv(self):
        admin = self.create_user(is_admin=True)
        member = self.create_user(level=1)
        self.login(admin)
        self.client.post(f"/admin/members/{member.id}/level", form={"level": "4"})

        page = self.client.get("/admin/audit")
        self.assertEqual(page.status, 200)
        self.assertIn(t("admin.audit.action.level_changed"), page.text)

        response = self.client.get("/admin/audit?format=csv")
        self.assertEqual(response.status, 200)
        self.assertIn("text/csv", response.header("Content-Type"))
        header = response.body.decode("utf-8-sig").splitlines()[0]
        self.assertIn(t("admin.audit.csv_action"), header)


# =============================================================================
# Settings
# =============================================================================


class SettingsTests(AdminUITestBase):
    _INT_KEYS = (
        "slot_minutes",
        "max_booking_minutes",
        "booking_horizon_days",
        "preemption_protection_minutes",
        "reminder_lead_minutes",
        "verify_token_hours",
        "invite_token_hours",
        "reset_token_hours",
        "daily_email_cap",
    )

    def _full_form(self) -> dict:
        settings = self.settings()
        form = {key: str(settings.values[key]) for key in self._INT_KEYS}
        form["default_open_time"] = settings.values["default_open_time"]
        form["default_close_time"] = settings.values["default_close_time"]
        if settings.values["reminders_enabled"]:
            form["reminders_enabled"] = "on"
        quotas = settings.values["quota_by_level"]
        for level in range(1, 11):
            form[f"quota_{level}"] = str(quotas.get(str(level), 0) or 0)
        return form

    def test_changing_a_setting_takes_effect_immediately(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)

        form = self._full_form()
        form["preemption_protection_minutes"] = "45"
        response = self.client.post("/admin/settings", form=form)
        self.assertEqual(response.status, 303)
        self.assertEqual(self.settings().preemption_protection_minutes, 45)

    def test_an_invalid_value_is_reported_and_not_saved(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)

        baseline = self.settings().preemption_protection_minutes

        bad = self._full_form()
        bad["preemption_protection_minutes"] = "999999999"
        response = self.client.post("/admin/settings", form=bad)
        self.assertEqual(response.status, 200)
        self.assertIn("notice-error", response.text)
        self.assertEqual(self.settings().preemption_protection_minutes, baseline)

    def test_settings_page_renders_every_key_with_an_explanation(self):
        admin = self.create_user(is_admin=True)
        self.login(admin)
        response = self.client.get("/admin/settings")
        self.assertEqual(response.status, 200)
        for key in self._INT_KEYS:
            self.assertIn(t(f"admin.settings.{key}.label"), response.text)
            self.assertIn(t(f"admin.settings.{key}.help"), response.text)


# =============================================================================
# Dashboard
# =============================================================================


class DashboardTests(AdminUITestBase):
    def test_dashboard_shows_pending_count_and_cron_state(self):
        admin = self.create_user(is_admin=True)
        self.create_user(status=models.PENDING_APPROVAL)
        self.create_user(status=models.PENDING_APPROVAL)
        self.login(admin)

        response = self.client.get("/admin")
        self.assertEqual(response.status, 200)
        self.assertIn(t("admin.dashboard.cron_never"), response.text)
        self.assertIn(t("admin.dashboard.pending_approvals_help", count=2), response.text)

        mailer.run_reminders(self.db)

        response2 = self.client.get("/admin")
        self.assertNotIn(t("admin.dashboard.cron_never"), response2.text)
