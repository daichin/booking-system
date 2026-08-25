"""Tests for app.services.accounts / app.services.sessions (spec §12 Group A)."""

from __future__ import annotations

import json
from datetime import timedelta

from app import models
from app.errors import (
    ACCOUNT_EXISTS,
    AppError,
    AuthError,
    EMAIL_NOT_VERIFIED,
    ForbiddenError,
    INVALID_STATUS_TRANSITION,
    INVITATION_NOT_FOUND,
    LOGIN_RATE_LIMITED,
    NOT_ADMIN,
    RateLimitError,
    TOKEN_EXPIRED,
    TOKEN_INVALID,
    TOKEN_USED,
)
from app.services import accounts, audit, sessions
from app.timeutil import now_utc
from tests.support import AppTestCase
from urllib.parse import parse_qs, urlparse


def _token_from_context(context: dict) -> str:
    """Extract the raw one-time token from a token-bearing link.

    Task 2's mailer contract requires accounts.py to hand over full URLs
    (``verify_url`` / ``invite_url`` / ``reset_url``), never a bare
    ``token`` key -- see app/services/email_templates.py's context
    contract. Tests pull the raw token back out of that URL so they can
    drive verify_email/accept_invitation/reset_password directly.
    """
    for key in ("verify_url", "invite_url", "reset_url"):
        url = context.get(key)
        if url:
            return parse_qs(urlparse(url).query)["token"][0]
    raise KeyError(f"no token-bearing url in context: {context!r}")


class AccountsTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        # accounts._dispatched is a module-level list shared across tests
        # running in this process; start each test with a clean slate.
        accounts.drain_dispatched()

    # --- helpers -------------------------------------------------------

    def make_admin(self) -> models.User:
        return self.create_user(is_admin=True, status=models.ACTIVE, level=10)

    def register(self, **overrides) -> accounts.RegisterResult:
        fields = dict(
            email="new-user@example.com",
            password="correct horse battery",
            full_name="王小明",
            department="研發部",
            phone="0912345678",
        )
        fields.update(overrides)
        return accounts.register(self.db, **fields)

    def expire_token(self, raw_token: str) -> None:
        from app import security

        def work(conn):
            conn.execute(
                "UPDATE email_tokens SET expires_at = ? WHERE token_hash = ?",
                (now_utc() - timedelta(hours=1), security.hash_token(raw_token)),
            )

        self.db.run_in_transaction(work)

    # --- A1 -------------------------------------------------------------

    def test_a1_self_registration_sends_e1_and_link_is_single_use(self):
        result = self.register()
        self.assertEqual(len(result.emails), 1)
        self.assertEqual(result.emails[0].kind, "E1")
        self.assertEqual(result.emails[0].to_email, "new-user@example.com")
        raw_token = _token_from_context(result.emails[0].context)

        user = accounts.verify_email(self.db, raw_token)
        self.assertEqual(user.status, models.PENDING_APPROVAL)
        self.assertIsNotNone(user.email_verified_at)

        with self.assertRaises(AppError) as ctx:
            accounts.verify_email(self.db, raw_token)
        self.assertErrorCode(ctx, TOKEN_USED)

    # --- A2 -------------------------------------------------------------

    def test_a2_expired_link_rejected_then_resend_issues_fresh_one(self):
        result = self.register()
        raw_token = _token_from_context(result.emails[0].context)
        self.expire_token(raw_token)

        with self.assertRaises(AppError) as ctx:
            accounts.verify_email(self.db, raw_token)
        self.assertErrorCode(ctx, TOKEN_EXPIRED)

        accounts.resend_verification(self.db, "new-user@example.com")
        events = accounts.drain_dispatched()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "E1")
        new_raw_token = _token_from_context(events[0].context)
        self.assertNotEqual(new_raw_token, raw_token)

        user = accounts.verify_email(self.db, new_raw_token)
        self.assertEqual(user.status, models.PENDING_APPROVAL)

    # --- A3 -------------------------------------------------------------

    def test_a3_verified_but_unapproved_cannot_book(self):
        result = self.register()
        raw_token = _token_from_context(result.emails[0].context)
        user = accounts.verify_email(self.db, raw_token)
        self.assertEqual(user.status, models.PENDING_APPROVAL)
        self.assertFalse(user.can_book)

    # --- A4 -------------------------------------------------------------

    def test_a4_approval_activates_and_sends_e2(self):
        admin = self.make_admin()
        result = self.register(email="approve-me@example.com")
        raw_token = _token_from_context(result.emails[0].context)
        pending = accounts.verify_email(self.db, raw_token)

        approved = accounts.approve(self.db, admin, pending.id)
        self.assertEqual(approved.status, models.ACTIVE)
        self.assertTrue(approved.can_book)

        events = accounts.drain_dispatched()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "E2")
        self.assertEqual(events[0].to_email, "approve-me@example.com")

        rows = self.query_all(
            "SELECT * FROM audit_log WHERE action = ?", (audit.USER_APPROVED,)
        )
        self.assertEqual(len(rows), 1)

    def test_approve_requires_admin(self):
        non_admin = self.create_user(status=models.ACTIVE)
        result = self.register(email="reject-me@example.com")
        pending = accounts.verify_email(self.db, _token_from_context(result.emails[0].context))
        with self.assertRaises(ForbiddenError) as ctx:
            accounts.approve(self.db, non_admin, pending.id)
        self.assertErrorCode(ctx, NOT_ADMIN)

    def test_reject_sends_e3_and_is_audited(self):
        admin = self.make_admin()
        result = self.register(email="reject-me2@example.com")
        pending = accounts.verify_email(self.db, _token_from_context(result.emails[0].context))

        rejected = accounts.reject(self.db, admin, pending.id)
        self.assertEqual(rejected.status, models.REJECTED)

        events = accounts.drain_dispatched()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "E3")

        rows = self.query_all(
            "SELECT * FROM audit_log WHERE action = ?", (audit.USER_REJECTED,)
        )
        self.assertEqual(len(rows), 1)

    # --- A5 -------------------------------------------------------------

    def test_a5_invited_user_registers_and_is_immediately_active(self):
        admin = self.make_admin()
        results = accounts.invite(self.db, admin, ["invitee@example.com"], 4)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(len(results[0].emails), 1)
        self.assertEqual(results[0].emails[0].kind, "E8")
        raw_token = _token_from_context(results[0].emails[0].context)

        user = accounts.accept_invitation(
            self.db,
            raw_token,
            password="correct horse battery",
            full_name="邀請成員",
            department="業務部",
            phone="0987654321",
        )
        self.assertEqual(user.status, models.ACTIVE)
        self.assertIsNotNone(user.email_verified_at)
        self.assertEqual(user.level, 4)
        self.assertTrue(user.can_book)
        self.assertEqual(user.email, "invitee@example.com")

        # No verification email, no approval step: accept_invitation must not
        # have dispatched anything at all.
        self.assertEqual(accounts.drain_dispatched(), [])

    def test_invite_requires_admin(self):
        non_admin = self.create_user(status=models.ACTIVE)
        with self.assertRaises(ForbiddenError):
            accounts.invite(self.db, non_admin, ["x@example.com"], None)

    def test_invite_defaults_to_level_one(self):
        admin = self.make_admin()
        results = accounts.invite(self.db, admin, ["default-level@example.com"], None)
        raw_token = _token_from_context(results[0].emails[0].context)
        user = accounts.accept_invitation(
            self.db,
            raw_token,
            password="correct horse battery",
            full_name="X",
            department="Y",
            phone="123",
        )
        self.assertEqual(user.level, models.MIN_LEVEL)

    def test_invite_refuses_email_with_existing_account(self):
        admin = self.make_admin()
        existing = self.create_user(email="taken@example.com", status=models.ACTIVE)
        results = accounts.invite(self.db, admin, [existing.email], None)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error, ACCOUNT_EXISTS)

    # --- A6 -------------------------------------------------------------

    def test_a6_revoked_invitation_rejected(self):
        admin = self.make_admin()
        results = accounts.invite(self.db, admin, ["revoke-me@example.com"], None)
        token_id = results[0].token_id
        raw_token = _token_from_context(results[0].emails[0].context)

        accounts.revoke_invitation(self.db, admin, token_id)

        with self.assertRaises(AppError) as ctx:
            accounts.accept_invitation(
                self.db,
                raw_token,
                password="correct horse battery",
                full_name="X",
                department="Y",
                phone="123",
            )
        self.assertErrorCode(ctx, TOKEN_INVALID)

    def test_a6_expired_invitation_rejected(self):
        admin = self.make_admin()
        results = accounts.invite(self.db, admin, ["expired-invite@example.com"], None)
        raw_token = _token_from_context(results[0].emails[0].context)
        self.expire_token(raw_token)

        with self.assertRaises(AppError) as ctx:
            accounts.accept_invitation(
                self.db,
                raw_token,
                password="correct horse battery",
                full_name="X",
                department="Y",
                phone="123",
            )
        self.assertErrorCode(ctx, TOKEN_EXPIRED)

    def test_revoke_invitation_unknown_id(self):
        admin = self.make_admin()
        with self.assertRaises(AppError) as ctx:
            accounts.revoke_invitation(self.db, admin, models.new_id())
        self.assertErrorCode(ctx, INVITATION_NOT_FOUND)

    def test_revoke_invitation_requires_admin(self):
        non_admin = self.create_user(status=models.ACTIVE)
        with self.assertRaises(ForbiddenError):
            accounts.revoke_invitation(self.db, non_admin, models.new_id())

    # --- A7 -------------------------------------------------------------

    def test_a7_password_reset_revokes_existing_sessions(self):
        user = self.create_user(
            email="carol@example.com", password="correct horse battery", status=models.ACTIVE
        )
        raw_cookie, _expires = sessions.create_session(self.db, user)
        self.assertIsNotNone(sessions.resolve_session(self.db, raw_cookie))

        accounts.request_password_reset(self.db, "carol@example.com")
        events = accounts.drain_dispatched()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "E9")
        raw_reset_token = _token_from_context(events[0].context)

        updated = accounts.reset_password(self.db, raw_reset_token, "new strong password")
        self.assertEqual(updated.id, user.id)

        self.assertIsNone(sessions.resolve_session(self.db, raw_cookie))
        # The new password works; the old one no longer does.
        relogged_in = accounts.authenticate(self.db, "carol@example.com", "new strong password")
        self.assertEqual(relogged_in.id, user.id)

    def test_reset_password_token_single_use(self):
        user = self.create_user(email="dana@example.com", status=models.ACTIVE)
        accounts.request_password_reset(self.db, "dana@example.com")
        raw_reset_token = _token_from_context(accounts.drain_dispatched()[0].context)

        accounts.reset_password(self.db, raw_reset_token, "brand new password")
        with self.assertRaises(AppError) as ctx:
            accounts.reset_password(self.db, raw_reset_token, "another new password")
        self.assertErrorCode(ctx, TOKEN_USED)

    def test_request_password_reset_unknown_email_is_silent(self):
        # Privacy: no error, nothing dispatched, for an address with no account.
        self.assertIsNone(accounts.request_password_reset(self.db, "nobody@example.com"))
        self.assertEqual(accounts.drain_dispatched(), [])

    def test_change_password_requires_current_password(self):
        user = self.create_user(password="correct horse battery", status=models.ACTIVE)
        with self.assertRaises(AuthError):
            accounts.change_password(self.db, user, "wrong password", "brand new password")
        updated = accounts.change_password(
            self.db, user, "correct horse battery", "brand new password"
        )
        self.assertFalse(updated.must_change_password)
        relogged_in = accounts.authenticate(self.db, user.email, "brand new password")
        self.assertEqual(relogged_in.id, user.id)

    # --- A8 -------------------------------------------------------------

    def test_a8_six_failed_logins_trip_rate_limit(self):
        user = self.create_user(
            email="bob@example.com", password="correct horse battery", status=models.ACTIVE
        )
        for _ in range(5):
            with self.assertRaises(AuthError) as ctx:
                accounts.authenticate(self.db, user.email, "wrong password")
            self.assertErrorCode(ctx, "INVALID_CREDENTIALS")

        with self.assertRaises(RateLimitError) as ctx:
            accounts.authenticate(self.db, user.email, "correct horse battery")
        self.assertErrorCode(ctx, LOGIN_RATE_LIMITED)

    def test_authenticate_blocks_unverified_suspended_and_rejected(self):
        pending = self.create_user(
            email="pending@example.com", password="correct horse battery",
            status=models.PENDING_EMAIL,
        )
        with self.assertRaises(AuthError) as ctx:
            accounts.authenticate(self.db, pending.email, "correct horse battery")
        self.assertErrorCode(ctx, EMAIL_NOT_VERIFIED)

        suspended = self.create_user(
            email="suspended@example.com", password="correct horse battery",
            status=models.SUSPENDED,
        )
        with self.assertRaises(AuthError) as ctx:
            accounts.authenticate(self.db, suspended.email, "correct horse battery")
        self.assertErrorCode(ctx, "ACCOUNT_SUSPENDED")

        rejected = self.create_user(
            email="rejected@example.com", password="correct horse battery",
            status=models.REJECTED,
        )
        with self.assertRaises(AuthError) as ctx:
            accounts.authenticate(self.db, rejected.email, "correct horse battery")
        self.assertErrorCode(ctx, "ACCOUNT_REJECTED")

    def test_authenticate_pending_approval_can_log_in(self):
        user = self.create_user(
            email="waiting@example.com", password="correct horse battery",
            status=models.PENDING_APPROVAL,
        )
        logged_in = accounts.authenticate(self.db, user.email, "correct horse battery")
        self.assertEqual(logged_in.status, models.PENDING_APPROVAL)

    def test_authenticate_reports_must_change_password_without_blocking(self):
        user = self.create_user(
            email="forced@example.com", password="correct horse battery",
            status=models.ACTIVE, must_change_password=True,
        )
        logged_in = accounts.authenticate(self.db, user.email, "correct horse battery")
        self.assertTrue(logged_in.must_change_password)

    # --- duplicate-registration privacy ---------------------------------

    def test_duplicate_registration_of_active_email_is_indistinguishable(self):
        existing = self.create_user(email="frank@example.com", status=models.ACTIVE)

        result = self.register(email=existing.email, full_name="不同的名字")

        # Same generic shape as a brand-new registration: no error, exactly
        # one email event queued.
        self.assertEqual(len(result.emails), 1)
        self.assertEqual(result.emails[0].kind, "E1_EXISTS")

        rows = self.query_all("SELECT id FROM users WHERE email = ?", (existing.email,))
        self.assertEqual(len(rows), 1)  # no duplicate row created

    def test_duplicate_registration_of_pending_email_resends(self):
        first = self.register(email="pending-dup@example.com")
        self.assertEqual(first.emails[0].kind, "E1")
        first_token = _token_from_context(first.emails[0].context)

        second = self.register(email="pending-dup@example.com")
        self.assertEqual(second.emails[0].kind, "E1")
        second_token = _token_from_context(second.emails[0].context)
        self.assertNotEqual(first_token, second_token)

        rows = self.query_all(
            "SELECT id FROM users WHERE email = ?", ("pending-dup@example.com",)
        )
        self.assertEqual(len(rows), 1)  # still only one account

    def test_register_email_rate_limit_applies_regardless_of_existence(self):
        # Three registrations for a brand-new address exhaust the budget;
        # a fourth is rate-limited exactly as it would be for an existing one.
        # register() only returns EmailEvents (CONTRACT.md §5: "for the
        # caller to enqueue after commit") -- it never writes email_log
        # itself, so this simulates the web layer's post-commit enqueue step
        # to make the shared email_log-backed rate limiter observe them.
        from app.services.mailer import enqueue

        address = "quota@example.com"
        for _ in range(3):
            result = self.register(email=address)
            enqueue(self.db, result.emails)
        with self.assertRaises(RateLimitError) as ctx:
            self.register(email=address)
        self.assertErrorCode(ctx, "EMAIL_RATE_LIMITED")

    # --- suspend / reactivate --------------------------------------------

    def test_suspend_and_reactivate_do_not_touch_bookings(self):
        admin = self.make_admin()
        member = self.create_user(status=models.ACTIVE)
        room = self.create_room()
        from tests.support import taipei_at

        booking = self.create_booking(
            room=room, user=member, start_at=taipei_at(2, 10), end_at=taipei_at(2, 11)
        )

        suspended = accounts.set_suspended(self.db, admin, member.id, True)
        self.assertEqual(suspended.status, models.SUSPENDED)
        still = self.get_booking(booking.id)
        self.assertEqual(still.status, models.CONFIRMED)  # spec §6.1: not auto-cancelled

        rows = self.query_all(
            "SELECT * FROM audit_log WHERE action = ? AND target_id = ?",
            (audit.USER_SUSPENDED, member.id),
        )
        self.assertEqual(len(rows), 1)

        reactivated = accounts.set_suspended(self.db, admin, member.id, False)
        self.assertEqual(reactivated.status, models.ACTIVE)
        rows = self.query_all(
            "SELECT * FROM audit_log WHERE action = ? AND target_id = ?",
            (audit.USER_REACTIVATED, member.id),
        )
        self.assertEqual(len(rows), 1)

    def test_suspend_requires_active_status(self):
        admin = self.make_admin()
        pending = self.create_user(status=models.PENDING_APPROVAL)
        with self.assertRaises(AppError) as ctx:
            accounts.set_suspended(self.db, admin, pending.id, True)
        self.assertErrorCode(ctx, INVALID_STATUS_TRANSITION)

    def test_set_suspended_requires_admin(self):
        non_admin = self.create_user(status=models.ACTIVE)
        member = self.create_user(status=models.ACTIVE)
        with self.assertRaises(ForbiddenError):
            accounts.set_suspended(self.db, non_admin, member.id, True)

    # --- level changes are audited (FR-3) ---------------------------------

    def test_level_change_is_audited(self):
        admin = self.make_admin()
        member = self.create_user(status=models.ACTIVE, level=2)

        updated = accounts.set_level(self.db, admin, member.id, 7)
        self.assertEqual(updated.level, 7)

        rows = self.query_all(
            "SELECT * FROM audit_log WHERE action = ? AND target_id = ?",
            (audit.LEVEL_CHANGED, member.id),
        )
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["from"], 2)
        self.assertEqual(detail["to"], 7)
        self.assertEqual(rows[0]["actor_user_id"], admin.id)

    def test_set_level_requires_admin(self):
        non_admin = self.create_user(status=models.ACTIVE)
        member = self.create_user(status=models.ACTIVE)
        with self.assertRaises(ForbiddenError):
            accounts.set_level(self.db, non_admin, member.id, 5)


class SessionsTests(AppTestCase):
    def test_create_resolve_revoke_round_trip(self):
        user = self.create_user(status=models.ACTIVE)
        raw_cookie, expires_at = sessions.create_session(self.db, user)
        self.assertGreater(expires_at, now_utc())

        resolved = sessions.resolve_session(self.db, raw_cookie)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, user.id)

        sessions.revoke_session(self.db, raw_cookie)
        self.assertIsNone(sessions.resolve_session(self.db, raw_cookie))

    def test_resolve_session_unknown_or_missing_cookie(self):
        self.assertIsNone(sessions.resolve_session(self.db, None))
        self.assertIsNone(sessions.resolve_session(self.db, "not-a-real-cookie"))

    def test_resolve_session_expired(self):
        user = self.create_user(status=models.ACTIVE)
        raw_cookie, _expires = sessions.create_session(self.db, user)

        def work(conn):
            from app import security

            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE id = ?",
                (now_utc() - timedelta(days=1), security.hash_token(raw_cookie)),
            )

        self.db.run_in_transaction(work)
        self.assertIsNone(sessions.resolve_session(self.db, raw_cookie))

    def test_revoke_all_for_user(self):
        user = self.create_user(status=models.ACTIVE)
        cookie_a, _ = sessions.create_session(self.db, user)
        cookie_b, _ = sessions.create_session(self.db, user)

        count = sessions.revoke_all_for_user(self.db, user.id)
        self.assertEqual(count, 2)
        self.assertIsNone(sessions.resolve_session(self.db, cookie_a))
        self.assertIsNone(sessions.resolve_session(self.db, cookie_b))
