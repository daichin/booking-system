"""Member UI and auth pages (Task 5).

Drives the WSGI app in-process through :class:`tests.webclient.Client`,
covering login/logout, registration privacy, the forced password-change
redirect, the day view, the pending-member read-only restriction, the
two-phase preemption UX (spec §7.2), cancellation, CSRF, and the
open-redirect guard on ``?next=``.
"""

from __future__ import annotations

from app import models
from app.config import Config
from app.services import accounts, sessions
from app.web.app import create_app
from tests.support import AppTestCase, taipei_at
from tests.webclient import Client

_PASSWORD = "correct horse battery"


class MemberUITestCase(AppTestCase):
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
        # Prime the double-submit CSRF cookie so every POST in a test can
        # simply include the token, the way a real browser that already
        # loaded a page would.
        self.client.get("/")

    def login(self, email: str, password: str = _PASSWORD):
        return self.client.post("/login", form={"email": email, "password": password})


# --- login / logout -----------------------------------------------------------


class LoginTests(MemberUITestCase):
    def test_login_sets_the_session_cookie_and_redirects_to_day(self):
        self.create_user(email="member@example.com", password=_PASSWORD)
        response = self.login("member@example.com")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/day")
        self.assertTrue(self.client.cookies.get("session"))

    def test_bad_password_is_rejected_without_a_session(self):
        self.create_user(email="member2@example.com", password=_PASSWORD)
        response = self.login("member2@example.com", password="wrong password")
        self.assertEqual(response.status, 401)
        self.assertNotIn("session", self.client.cookies)

    def test_logout_clears_the_session_cookie(self):
        self.create_user(email="member3@example.com", password=_PASSWORD)
        self.login("member3@example.com")
        self.assertTrue(self.client.cookies.get("session"))
        response = self.client.post("/logout", form={})
        self.assertEqual(response.status, 303)
        self.assertNotIn("session", self.client.cookies)
        # And the session no longer authenticates a page that needs one.
        day = self.client.get("/day")
        self.assertEqual(day.status, 303)
        self.assertEqual(day.location, "/login?next=/day")


# --- registration privacy ------------------------------------------------------


class RegistrationTests(MemberUITestCase):
    def _register(self, email: str):
        return self.client.post(
            "/register",
            form={
                "email": email,
                "password": _PASSWORD,
                "full_name": "王小明",
                "department": "業務部",
                "phone": "0912345678",
            },
        )

    def test_same_response_for_a_new_address_and_an_existing_active_one(self):
        self.create_user(email="already-active@example.com", status=models.ACTIVE)

        fresh = self._register("brand-new@example.com")
        existing = self._register("already-active@example.com")

        self.assertEqual(fresh.status, 200)
        self.assertEqual(existing.status, 200)
        # Uniform wording -- the UI must never confirm or deny existence.
        from app.i18n import t

        message = t("auth.register.success")
        self.assertIn(message, fresh.text)
        self.assertIn(message, existing.text)

    def test_a_new_registration_actually_creates_a_pending_email_user(self):
        self._register("new-user@example.com")
        row = self.query_one("SELECT * FROM users WHERE email = ?", ("new-user@example.com",))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], models.PENDING_EMAIL)


# --- forced password change (spec §10.3) ----------------------------------------


class ForcedPasswordChangeTests(MemberUITestCase):
    def test_must_change_password_redirects_day_to_password_and_can_be_cleared(self):
        self.create_user(
            email="forced@example.com",
            password=_PASSWORD,
            must_change_password=True,
        )
        self.login("forced@example.com")

        redirected = self.client.get("/day")
        self.assertEqual(redirected.status, 303)
        self.assertEqual(redirected.location, "/password")

        change = self.client.post(
            "/password",
            form={
                "current_password": _PASSWORD,
                "new_password": "a whole new password",
                "confirm_new_password": "a whole new password",
            },
        )
        self.assertEqual(change.status, 303)
        self.assertEqual(change.location, "/day")

        now_ok = self.client.get("/day")
        self.assertEqual(now_ok.status, 200)


# --- day view --------------------------------------------------------------------


class DayViewTests(MemberUITestCase):
    def test_day_view_shows_an_existing_booking(self):
        user = self.create_user(email="viewer@example.com", password=_PASSWORD, level=3)
        room = self.create_room(name="第一會議室")
        start_at = taipei_at(1, 10, 0)
        end_at = taipei_at(1, 11, 0)
        self.create_booking(room=room, user=user, start_at=start_at, end_at=end_at, title="產品會議")

        self.login("viewer@example.com")
        response = self.client.get(f"/day?date={start_at.date()}")
        self.assertEqual(response.status, 200)
        self.assertIn("產品會議", response.text)
        self.assertIn("第一會議室", response.text)
        self.assertIn(user.full_name, response.text)


class PendingMemberTests(MemberUITestCase):
    def test_pending_member_sees_day_read_only_and_cannot_book(self):
        self.create_user(
            email="pending@example.com", password=_PASSWORD, status=models.PENDING_APPROVAL
        )
        room = self.create_room(name="等待室")
        self.login("pending@example.com")

        response = self.client.get("/day")
        self.assertEqual(response.status, 200)
        from app.i18n import error_message

        self.assertIn(error_message("AWAITING_APPROVAL"), response.text)
        # No quick-book form is offered.
        self.assertNotIn('action="/bookings"', response.text)

        start_at = taipei_at(1, 10, 0)
        end_at = taipei_at(1, 11, 0)
        blocked = self.client.post(
            "/api/bookings",
            json_body={
                "room_id": room.id,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "title": "偷偷開會",
            },
        )
        self.assertEqual(blocked.status, 403)
        self.assertEqual(blocked.json()["error"], "AWAITING_APPROVAL")


# --- booking creation and the two-phase preemption UX ---------------------------


class BookingApiTests(MemberUITestCase):
    def test_creating_a_booking_via_the_json_api(self):
        self.create_user(email="booker@example.com", password=_PASSWORD, level=2)
        room = self.create_room(name="API 會議室")
        self.login("booker@example.com")

        start_at = taipei_at(2, 9, 0)
        end_at = taipei_at(2, 10, 0)
        response = self.client.post(
            "/api/bookings",
            json_body={
                "room_id": room.id,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "title": "API 測試會議",
            },
        )
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(payload["outcome"], "CREATED")
        self.assertEqual(
            int(self.query_one("SELECT COUNT(*) AS n FROM bookings")["n"]), 1
        )

    def test_two_phase_flow_requires_confirmation_before_preempting(self):
        low = self.create_user(email="low@example.com", password=_PASSWORD, level=1)
        high = self.create_user(email="high@example.com", password=_PASSWORD, level=5)
        room = self.create_room(name="搶奪會議室")
        start_at = taipei_at(3, 10, 0)
        end_at = taipei_at(3, 11, 0)
        victim = self.create_booking(room=room, user=low, start_at=start_at, end_at=end_at, title="低權限會議")

        self.login("high@example.com")
        body = {
            "room_id": room.id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "title": "高權限會議",
        }

        check = self.client.post("/api/bookings/check", json_body=body)
        self.assertEqual(check.json()["outcome"], "PREEMPTION_REQUIRED")

        # Phase 2 without confirmation must not create or displace anything.
        unconfirmed = self.client.post("/api/bookings", json_body=body)
        self.assertEqual(unconfirmed.json()["outcome"], "PREEMPTION_REQUIRED")
        self.assertEqual(self.get_booking(victim.id).status, models.CONFIRMED)

        confirmed_body = dict(body, confirm_preemption=True)
        confirmed = self.client.post("/api/bookings", json_body=confirmed_body)
        payload = confirmed.json()
        self.assertEqual(payload["outcome"], "CREATED")
        self.assertEqual(self.get_booking(victim.id).status, models.PREEMPTED)

    def test_blocked_response_never_exposes_the_blocking_members_email(self):
        blocker = self.create_user(
            email="blocker-secret@example.com", password=_PASSWORD, level=2,
            full_name="陳大文", department="財務部",
        )
        challenger = self.create_user(email="challenger@example.com", password=_PASSWORD, level=2)
        room = self.create_room(name="同等級會議室")
        start_at = taipei_at(4, 14, 0)
        end_at = taipei_at(4, 15, 0)
        self.create_booking(room=room, user=blocker, start_at=start_at, end_at=end_at, title="財務會議")

        self.login("challenger@example.com")
        body = {
            "room_id": room.id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "title": "挑戰會議",
        }
        response = self.client.post("/api/bookings/check", json_body=body)
        payload = response.json()
        self.assertEqual(payload["outcome"], "BLOCKED")
        self.assertEqual(payload["reason"], "EQUAL_OR_HIGHER_LEVEL")
        self.assertNotIn("blocker-secret@example.com", response.text)
        self.assertIn("陳大文", response.text)

        # And the non-JS, server-rendered path is equally careful.
        form_response = self.client.post(
            "/bookings",
            form={
                "room_id": room.id,
                "date": start_at.date().isoformat(),
                "start_time": "14:00",
                "end_time": "15:00",
                "title": "挑戰會議",
            },
        )
        self.assertEqual(form_response.status, 409)
        self.assertNotIn("blocker-secret@example.com", form_response.text)
        self.assertIn("陳大文", form_response.text)

    def test_non_js_form_path_creates_a_booking_when_available(self):
        self.create_user(email="noscript@example.com", password=_PASSWORD, level=2)
        room = self.create_room(name="無 JS 會議室")
        self.login("noscript@example.com")

        start_at = taipei_at(5, 9, 0)
        response = self.client.post(
            "/bookings",
            form={
                "room_id": room.id,
                "date": start_at.date().isoformat(),
                "start_time": "09:00",
                "end_time": "10:00",
                "title": "無 JS 測試會議",
            },
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/my?booked=1")
        self.assertEqual(
            int(self.query_one("SELECT COUNT(*) AS n FROM bookings")["n"]), 1
        )

    def test_non_js_form_path_shows_a_confirmation_screen_for_preemption(self):
        low = self.create_user(email="low2@example.com", password=_PASSWORD, level=1)
        self.create_user(email="high2@example.com", password=_PASSWORD, level=6)
        room = self.create_room(name="無 JS 搶奪會議室")
        start_at = taipei_at(6, 10, 0)
        end_at = taipei_at(6, 11, 0)
        self.create_booking(room=room, user=low, start_at=start_at, end_at=end_at, title="小會議")

        self.login("high2@example.com")
        response = self.client.post(
            "/bookings",
            form={
                "room_id": room.id,
                "date": start_at.date().isoformat(),
                "start_time": "10:00",
                "end_time": "11:00",
                "title": "大會議",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIn('name="confirm_preemption"', response.text)
        self.assertIn("無 JS 搶奪會議室", response.text)
        # Nothing has been created or displaced yet.
        self.assertEqual(
            int(self.query_one("SELECT COUNT(*) AS n FROM bookings")["n"]), 1
        )


# --- cancellation ----------------------------------------------------------------


class CancelTests(MemberUITestCase):
    def test_owner_can_cancel_from_my_bookings(self):
        user = self.create_user(email="owner@example.com", password=_PASSWORD)
        room = self.create_room()
        start_at = taipei_at(1, 13, 0)
        end_at = taipei_at(1, 14, 0)
        booking = self.create_booking(room=room, user=user, start_at=start_at, end_at=end_at)

        self.login("owner@example.com")
        response = self.client.post(f"/bookings/{booking.id}/cancel", form={})
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/my?cancelled=1")
        self.assertEqual(self.get_booking(booking.id).status, models.CANCELLED_BY_USER)

    def test_a_stranger_cannot_cancel_someone_elses_booking(self):
        owner = self.create_user(email="realowner@example.com", password=_PASSWORD)
        stranger = self.create_user(email="stranger@example.com", password=_PASSWORD)
        room = self.create_room()
        start_at = taipei_at(1, 15, 0)
        end_at = taipei_at(1, 16, 0)
        booking = self.create_booking(room=room, user=owner, start_at=start_at, end_at=end_at)

        self.login("stranger@example.com")
        response = self.client.post(f"/api/bookings/{booking.id}/cancel", form={})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.get_booking(booking.id).status, models.CONFIRMED)


# --- CSRF and open redirect -------------------------------------------------------


class SecurityTests(MemberUITestCase):
    def test_a_post_without_the_csrf_token_is_rejected(self):
        # A first GET issues the csrf cookie; the raw request below omits it
        # from the body/header entirely, which is what a forged cross-site
        # request would look like.
        self.client.get("/login")
        response = self.client.post(
            "/register",
            form={
                "email": "no-csrf@example.com",
                "password": _PASSWORD,
                "full_name": "無 CSRF",
                "department": "部門",
                "phone": "000",
            },
            csrf=False,
        )
        self.assertEqual(response.status, 403)

    def test_open_redirect_via_next_is_rejected(self):
        self.create_user(email="safe@example.com", password=_PASSWORD)
        response = self.client.post(
            "/login",
            form={
                "email": "safe@example.com",
                "password": _PASSWORD,
                "next": "https://evil.example.com/steal",
            },
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/day")

    def test_protocol_relative_next_is_also_rejected(self):
        self.create_user(email="safe2@example.com", password=_PASSWORD)
        response = self.client.post(
            "/login",
            form={
                "email": "safe2@example.com",
                "password": _PASSWORD,
                "next": "//evil.example.com/steal",
            },
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/day")


# --- email verification (spec §12 A2) ---------------------------------------------


class VerifyTests(MemberUITestCase):
    def test_expired_link_offers_a_working_resend(self):
        result = accounts.register(
            self.db,
            email="expiring@example.com",
            password=_PASSWORD,
            full_name="測試",
            department="部門",
            phone="000",
        )
        raw_token = result.emails[0].context["verify_url"].rsplit("token=", 1)[1]

        settings = self.settings()
        self.freeze(taipei_at(settings.values["verify_token_hours"] // 24 + 2, 0, 0))

        expired = self.client.get(f"/verify?token={raw_token}")
        self.assertEqual(expired.status, 400)
        self.assertIn('action="/verify"', expired.text)

        before = int(self.query_one(
            "SELECT COUNT(*) AS n FROM email_tokens WHERE email = ? AND type = 'verify_email'",
            ("expiring@example.com",),
        )["n"])

        resent = self.client.post("/verify", form={"email": "expiring@example.com"})
        self.assertEqual(resent.status, 200)

        after = int(self.query_one(
            "SELECT COUNT(*) AS n FROM email_tokens WHERE email = ? AND type = 'verify_email'",
            ("expiring@example.com",),
        )["n"])
        self.assertEqual(after, before + 1)
