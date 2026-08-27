"""Tests for Task 2 -- the email service (spec §9, acceptance §12 Group D).

Uses :class:`app.services.transports.FakeTransport` exclusively; the Brevo
transport is exercised with the network layer mocked out, so no test ever
sends a real message or needs a provider account.
"""

from __future__ import annotations

import unittest
import urllib.error
from datetime import timedelta
from unittest.mock import MagicMock, patch

from app import i18n, models
from app.services import email_templates, mailer
from app.services.transports import BrevoTransport, FakeTransport, Message
from app.timeutil import now_utc
from tests.support import AppTestCase

_ALL_KINDS = [
    "E1", "E1_EXISTS", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "E10",
]
_KINDS_WITH_TIME = {"E4", "E5", "E6", "E10"}

#: The footer line and the timezone label, per locale. Every template must
#: carry the footer, and every template that states a time must label the
#: timezone -- a cross-cutting rule, so it is checked in both languages
#: rather than in whichever one happens to be ambient. Pinning these to the
#: default locale alone would have let a missing zh-TW string ship.
_MARKERS = {
    "en": ("automated notification", "Taipei time"),
    "zh-TW": ("自動發送", "台北時間"),
}


def _sample_context(kind: str) -> dict:
    start = now_utc() + timedelta(days=1, hours=2)
    end = start + timedelta(hours=1)
    time_fields = {"start_at": start, "end_at": end}

    if kind == "E1":
        return {
            "full_name": "王小明",
            "verify_url": "https://example.onrender.com/verify?token=abc",
            "expires_hours": 24,
        }
    if kind == "E1_EXISTS":
        return {"login_url": "https://example.onrender.com/login"}
    if kind == "E2":
        return {"full_name": "王小明", "login_url": "https://example.onrender.com/login"}
    if kind == "E3":
        return {"full_name": "王小明"}
    if kind == "E4":
        return {
            "full_name": "王小明", "room_name": "會議室 A", "title": "週會",
            "cancel_url": "https://example.onrender.com/my", **time_fields,
        }
    if kind == "E5":
        return {
            "full_name": "王小明", "room_name": "會議室 A", "title": "週會",
            "reason": email_templates.E5_PREEMPTED,
            "book_url": "https://example.onrender.com/day", **time_fields,
        }
    if kind == "E6":
        return {"full_name": "王小明", "room_name": "會議室 A", "title": "週會", **time_fields}
    if kind == "E7":
        return {
            "admin_name": "管理員",
            "pending": [
                {
                    "full_name": "陳大文", "department": "業務部",
                    "phone": "0912345678", "email": "chen@example.com",
                },
            ],
            "admin_url": "https://example.onrender.com/admin/approvals",
        }
    if kind == "E8":
        return {
            "invite_url": "https://example.onrender.com/invite?token=xyz",
            "expires_hours": 168,
        }
    if kind == "E9":
        return {
            "full_name": "王小明",
            "reset_url": "https://example.onrender.com/reset?token=zzz",
            "expires_hours": 2,
        }
    if kind == "E10":
        return {"full_name": "王小明", "room_name": "會議室 A", "title": "週會", **time_fields}
    raise ValueError(kind)


class EmailTemplateTests(unittest.TestCase):
    """Pure rendering checks -- no database needed."""

    def setUp(self) -> None:
        ambient = i18n.current_locale()
        self.addCleanup(i18n.set_locale, ambient)

    def test_every_catalogue_kind_renders_with_both_parts(self) -> None:
        for locale, (footer, timezone) in _MARKERS.items():
            i18n.set_locale(locale)
            for kind in _ALL_KINDS:
                with self.subTest(locale=locale, kind=kind):
                    rendered = email_templates.render(kind, _sample_context(kind))
                    self.assertTrue(rendered.subject.strip())
                    self.assertTrue(rendered.text.strip())
                    self.assertTrue(rendered.html.strip())
                    self.assertIn("<html", rendered.html.lower())
                    self.assertIn(footer, rendered.text)
                    if kind in _KINDS_WITH_TIME:
                        self.assertIn(timezone, rendered.text)
                        self.assertIn(timezone, rendered.html)

    def test_e5_preemption_never_names_the_preempting_member(self) -> None:
        """Spec §7.1 / §12 C11: E5 must not disclose who preempted the victim."""
        i18n.set_locale("zh-TW")
        context = _sample_context("E5")
        rendered = email_templates.render("E5", context)

        winner_name = "覆蓋者王五"
        winner_email = "winner@example.com"
        self.assertNotIn(winner_name, rendered.text)
        self.assertNotIn(winner_name, rendered.html)
        self.assertNotIn(winner_email, rendered.text)
        self.assertNotIn(winner_email, rendered.html)
        # It still names the room and time, and offers a rebooking link.
        self.assertIn("會議室 A", rendered.text)
        self.assertIn("台北時間", rendered.text)
        self.assertIn("https://example.onrender.com/day", rendered.text)

    def test_e5_admin_and_room_reasons_also_render(self) -> None:
        for locale, (_footer, timezone) in _MARKERS.items():
            i18n.set_locale(locale)
            for reason in (email_templates.E5_ADMIN, email_templates.E5_ROOM):
                with self.subTest(locale=locale, reason=reason):
                    context = _sample_context("E5")
                    context["reason"] = reason
                    rendered = email_templates.render("E5", context)
                    self.assertTrue(rendered.subject.strip())
                    self.assertIn(timezone, rendered.text)


class MailerTests(AppTestCase):
    """§12 Group D acceptance scenarios, using FakeTransport."""

    def test_d1_reminder_fires_exactly_once_per_booking(self) -> None:
        room = self.create_room()
        user = self.create_user(full_name="王小明")
        start = now_utc() + timedelta(minutes=30)
        end = start + timedelta(hours=1)
        self.create_booking(room=room, user=user, start_at=start, end_at=end)

        fake = FakeTransport()
        first = mailer.run_reminders(self.db, transport=fake)
        self.assertEqual(first.sent, 1)
        self.assertEqual(len(fake.sent), 1)

        second = mailer.run_reminders(self.db, transport=fake)
        self.assertEqual(second.sent, 0)
        self.assertEqual(len(fake.sent), 1)  # not double-sent

        rows = self.query_all("SELECT * FROM email_log WHERE type = 'E10'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "sent")
        self.assertEqual(rows[0]["dedupe_key"], f"reminder:{rows[0]['related_booking_id']}")

        cron_rows = self.query_all(
            "SELECT * FROM cron_runs WHERE job = 'send_reminders' ORDER BY started_at"
        )
        self.assertEqual(len(cron_rows), 2)
        self.assertTrue(all(row["ok"] for row in cron_rows))

    def test_d2_cancelled_booking_reminder_is_not_sent(self) -> None:
        room = self.create_room()
        user = self.create_user()
        start = now_utc() + timedelta(minutes=30)
        end = start + timedelta(hours=1)
        booking = self.create_booking(room=room, user=user, start_at=start, end_at=end)
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE bookings SET status = 'cancelled_by_user' WHERE id = ?",
                (booking.id,),
            )
        )

        fake = FakeTransport()
        report = mailer.run_reminders(self.db, transport=fake)

        self.assertEqual(report.sent, 0)
        self.assertEqual(len(fake.sent), 0)
        self.assertEqual(
            len(self.query_all("SELECT * FROM email_log WHERE type = 'E10'")), 0
        )

    def test_reminders_respect_kill_switch(self) -> None:
        self.set_setting("reminders_enabled", False)
        room = self.create_room()
        user = self.create_user()
        start = now_utc() + timedelta(minutes=30)
        end = start + timedelta(hours=1)
        self.create_booking(room=room, user=user, start_at=start, end_at=end)

        fake = FakeTransport()
        report = mailer.run_reminders(self.db, transport=fake)

        self.assertEqual(report.sent, 0)
        self.assertEqual(len(fake.sent), 0)
        cron_rows = self.query_all("SELECT * FROM cron_runs WHERE job = 'send_reminders'")
        self.assertEqual(len(cron_rows), 1)
        self.assertEqual(cron_rows[0]["detail"], "reminders_disabled")

    def test_d3_daily_cap_drops_reminders_but_still_sends_critical_mail(self) -> None:
        self.set_setting("daily_email_cap", 0)
        user = self.create_user(full_name="王小明")
        start = now_utc() + timedelta(days=1)
        end = start + timedelta(hours=1)

        events = [
            mailer.EmailEvent(
                kind="E1", to_email=user.email,
                context={
                    "full_name": user.full_name,
                    "verify_url": "https://example.onrender.com/verify?token=a",
                    "expires_hours": 24,
                },
            ),
            mailer.EmailEvent(
                kind="E5", to_email=user.email,
                context={
                    "full_name": user.full_name, "room_name": "會議室 A",
                    "title": "週會", "reason": email_templates.E5_PREEMPTED,
                    "start_at": start, "end_at": end,
                },
            ),
            mailer.EmailEvent(
                kind="E8", to_email="invitee@example.com",
                context={
                    "invite_url": "https://example.onrender.com/invite?token=b",
                    "expires_hours": 168,
                },
            ),
            mailer.EmailEvent(
                kind="E9", to_email=user.email,
                context={
                    "full_name": user.full_name,
                    "reset_url": "https://example.onrender.com/reset?token=c",
                    "expires_hours": 2,
                },
            ),
            mailer.EmailEvent(
                kind="E10", to_email=user.email,
                context={
                    "full_name": user.full_name, "room_name": "會議室 A",
                    "title": "週會", "start_at": start, "end_at": end,
                },
            ),
        ]

        fake = FakeTransport()
        ids = mailer.enqueue(self.db, events)
        self.assertEqual(len(ids), 5)

        report = mailer.send_pending(self.db, transport=fake, limit=10)

        self.assertEqual(report.sent, 4)
        self.assertEqual(report.skipped, 1)
        statuses = {
            row["type"]: row["status"]
            for row in self.query_all("SELECT type, status FROM email_log")
        }
        self.assertEqual(statuses["E1"], "sent")
        self.assertEqual(statuses["E5"], "sent")
        self.assertEqual(statuses["E8"], "sent")
        self.assertEqual(statuses["E9"], "sent")
        self.assertEqual(statuses["E10"], "skipped")

    def test_d4_two_registrations_within_an_hour_produce_one_digest(self) -> None:
        self.create_user(is_admin=True, level=10, email="admin@example.com")
        self.create_user(
            status=models.PENDING_APPROVAL, full_name="陳大文", email="chen@example.com"
        )

        fake = FakeTransport()
        first = mailer.run_admin_digest(self.db, transport=fake)
        self.assertEqual(first, 1)
        self.assertEqual(len(fake.sent), 1)

        self.create_user(
            status=models.PENDING_APPROVAL, full_name="林小華", email="lin@example.com"
        )
        second = mailer.run_admin_digest(self.db, transport=fake)
        self.assertEqual(second, 0)
        self.assertEqual(len(fake.sent), 1)  # still just the one batched digest

        digest_rows = self.query_all("SELECT * FROM email_log WHERE type = 'E7'")
        self.assertEqual(len(digest_rows), 1)
        # It lists both pending registrations even though only one triggered it.
        self.assertIn("陳大文", fake.sent[0].text)

    def test_d5_provider_failure_is_retried_then_marked_failed(self) -> None:
        user = self.create_user(full_name="王小明")
        start = now_utc() + timedelta(days=1)
        end = start + timedelta(hours=1)
        event = mailer.EmailEvent(
            kind="E4", to_email=user.email,
            context={
                "full_name": user.full_name, "room_name": "會議室 A",
                "title": "週會", "start_at": start, "end_at": end,
            },
        )

        fake = FakeTransport()
        fake.fail_next(mailer.MAX_ATTEMPTS)

        ids = mailer.enqueue(self.db, [event])
        row_id = ids[0]

        for _ in range(mailer.MAX_ATTEMPTS):
            report = mailer.send_pending(self.db, transport=fake, limit=10)
            self.assertEqual(report.sent, 0)

        row = self.query_one("SELECT * FROM email_log WHERE id = ?", (row_id,))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], mailer.MAX_ATTEMPTS)
        self.assertEqual(row["error"], "simulated failure")
        self.assertEqual(len(fake.sent), 0)

        # A further call must not retry a terminally-failed row.
        report = mailer.send_pending(self.db, transport=fake, limit=10)
        self.assertEqual(report.sent, 0)
        self.assertEqual(report.failed, 0)

    def test_enqueue_dedupe_key_prevents_duplicate_rows(self) -> None:
        user = self.create_user()
        event = lambda: mailer.EmailEvent(  # noqa: E731 - local convenience
            kind="E10", to_email=user.email,
            context={
                "full_name": user.full_name, "room_name": "R", "title": "T",
                "start_at": now_utc(), "end_at": now_utc() + timedelta(hours=1),
            },
            dedupe_key="reminder:shared",
        )
        first_ids = mailer.enqueue(self.db, [event()])
        second_ids = mailer.enqueue(self.db, [event()])
        self.assertEqual(len(first_ids), 1)
        self.assertEqual(len(second_ids), 0)
        rows = self.query_all(
            "SELECT * FROM email_log WHERE dedupe_key = 'reminder:shared'"
        )
        self.assertEqual(len(rows), 1)


class BrevoTransportTests(unittest.TestCase):
    """Contract tests against a stubbed HTTP layer -- no real network."""

    def _config(self):
        from app.config import Config

        return Config(
            email_api_key="key123",
            email_from="noreply@example.com",
            email_from_name="會議室預約系統",
            email_transport="brevo",
        )

    def test_success_response_is_parsed(self) -> None:
        transport = BrevoTransport(self._config())
        message = Message(
            to_email="user@example.com", to_name="王小明",
            subject="主旨", html="<p>內容</p>", text="內容",
        )

        fake_response = MagicMock()
        fake_response.getcode.return_value = 201
        fake_response.read.return_value = b'{"messageId": "abc123"}'
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=fake_response) as mock_open:
            result = transport.send(message)

        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, "abc123")
        mock_open.assert_called_once()
        request = mock_open.call_args[0][0]
        self.assertEqual(request.get_header("Api-key"), "key123")
        self.assertIn(b"noreply@example.com", request.data)
        self.assertIn(b"user@example.com", request.data)

    def test_http_error_becomes_a_result_not_an_exception(self) -> None:
        transport = BrevoTransport(self._config())
        message = Message(to_email="user@example.com", subject="s", html="h", text="t")

        http_error = urllib.error.HTTPError(
            "https://api.brevo.com/v3/smtp/email", 400, "Bad Request",
            hdrs=None, fp=MagicMock(read=lambda: b"bad key"),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = transport.send(message)

        self.assertFalse(result.ok)
        self.assertIn("http_400", result.error)

    def test_network_error_becomes_a_result_not_an_exception(self) -> None:
        transport = BrevoTransport(self._config())
        message = Message(to_email="user@example.com", subject="s", html="h", text="t")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("no network on this machine"),
        ):
            result = transport.send(message)

        self.assertFalse(result.ok)
        self.assertIn("network_error", result.error)


if __name__ == "__main__":
    unittest.main()
