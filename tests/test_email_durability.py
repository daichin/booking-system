"""Queued mail must survive the process that queued it.

The render context -- the values a template needs to produce a message --
used to live only in a dict in the web process. Render's free tier sleeps
that process after fifteen minutes idle, and the only thing that flushed the
queue was a cron call on the same cadence, so the sending process was usually
a *fresh* one with an empty dict. Every such message was marked ``failed``
with ``context_unavailable``, without consuming a single retry and with no
way to rebuild it. E5 "your booking was preempted" notices were among the
mail lost that way, which the spec requires be delivered.

These tests hold the line on the fix: the row alone must be enough to send.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

from app import models
from app.config import Config
from app.errors import ConflictError, NotFoundError
from app.services import mailer
from app.services.transports import FakeTransport
from app.settings import Settings
from app.timeutil import now_utc
from app.web.app import create_app
from tests.support import AppTestCase, taipei_at
from tests.test_email import _ALL_KINDS, _sample_context
from tests.webclient import Client

_PASSWORD = "a decent passphrase"


class DurabilityTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.room = self.create_room(name="Meeting Room 1")
        self.member = self.create_user(
            email="member@example.com", password=_PASSWORD, full_name="Chris V"
        )
        self.start = taipei_at(1, 10)

    def booking_event(self, kind: str = "E4", **extra) -> mailer.EmailEvent:
        return mailer.EmailEvent(
            kind=kind,
            to_email="member@example.com",
            context={
                "full_name": "Chris V",
                "room_name": "Meeting Room 1",
                "title": "Quarterly planning review",
                "start_at": self.start,
                "end_at": self.start + timedelta(hours=1),
                **extra,
            },
        )

    def log_rows(self) -> list[dict]:
        return self.query_all("SELECT * FROM email_log ORDER BY created_at")


class ContextSurvivesTheProcessTests(DurabilityTestBase):
    def test_the_row_carries_everything_needed_to_send_it(self):
        mailer.enqueue(self.db, [self.booking_event()])
        row = self.log_rows()[0]

        self.assertIsNotNone(row["context"], "the row cannot be rendered on its own")
        decoded = mailer._decode_context(row["context"])
        self.assertEqual(decoded["room_name"], "Meeting Room 1")
        self.assertEqual(decoded["start_at"], self.start)

    def test_a_restarted_process_can_still_send_what_the_old_one_queued(self):
        """The exact regression. Reloading the module is a fresh namespace:
        anything the sender needed to remember in memory is gone."""
        mailer.enqueue(self.db, [self.booking_event()])

        reloaded = importlib.reload(mailer)
        self.addCleanup(importlib.reload, mailer)

        transport = FakeTransport()
        report = reloaded.send_pending(self.db, transport=transport)

        self.assertEqual(report.sent, 1, "the message died with its process")
        self.assertEqual(report.failed, 0)
        self.assertEqual(len(transport.sent), 1)

    def test_the_sender_keeps_no_per_row_state_at_all(self):
        """A guard against the cache being reintroduced.

        The bug was not that the cache was too small or too short-lived; it
        was that any in-process store is wrong here, because the process that
        queues a message is routinely not the one that sends it.
        """
        for name in ("_CONTEXT_CACHE", "_remember", "_recall", "_forget"):
            self.assertFalse(
                hasattr(mailer, name),
                f"mailer.{name} is back -- queued mail is process-bound again",
            )

    def test_context_unavailable_is_now_only_reachable_for_legacy_rows(self):
        """A row written before the column existed still fails honestly."""
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " attempts, created_at) VALUES (?, ?, ?, ?, 'queued', 0, ?)",
                (models.new_id(), "old@example.com", "E4", "Old", now_utc()),
            )
        )
        report = mailer.send_pending(self.db, transport=FakeTransport())

        self.assertEqual(report.sent, 0)
        row = self.query_one(
            "SELECT status, error FROM email_log WHERE to_email = ?",
            ("old@example.com",),
        )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "context_unavailable")


class ContextEncodingTests(AppTestCase):
    def test_every_catalogue_kind_round_trips_exactly(self):
        """Scanner, not a sample: a new template that puts an unencodable
        value in its context would otherwise fail in production, at enqueue
        time, inside somebody's booking request."""
        for kind in _ALL_KINDS:
            with self.subTest(kind=kind):
                original = _sample_context(kind)
                restored = mailer._decode_context(mailer._encode_context(original))
                self.assertEqual(restored, original)

    def test_datetimes_come_back_as_datetimes_not_strings(self):
        """A template formatting a raw ISO string would be a silent
        corruption of every time shown in every email."""
        moment = now_utc()
        restored = mailer._decode_context(mailer._encode_context({"at": moment}))
        self.assertEqual(restored["at"], moment)
        self.assertEqual(restored["at"].utcoffset(), moment.utcoffset())

    def test_a_value_it_cannot_store_is_refused_loudly(self):
        with self.assertRaises(TypeError):
            mailer._encode_context({"bad": object()})


class ScrubbingTests(DurabilityTestBase):
    """A delivered one-time link must not stay in the log."""

    def test_a_token_email_drops_its_context_once_delivered(self):
        secret = "https://example.test/reset?token=THE-SECRET"
        mailer.enqueue(
            self.db,
            [
                mailer.EmailEvent(
                    kind="E9",
                    to_email="member@example.com",
                    context={
                        "full_name": "Chris V",
                        "reset_url": secret,
                        "expires_hours": 2,
                    },
                )
            ],
        )
        self.assertIn(secret, self.log_rows()[0]["context"])

        mailer.send_pending(self.db, transport=FakeTransport())

        row = self.log_rows()[0]
        self.assertEqual(row["status"], "sent")
        self.assertIsNone(row["context"], "a live reset link is still in the log")

    def test_an_ordinary_email_keeps_its_context_so_it_can_be_resent(self):
        mailer.enqueue(self.db, [self.booking_event()])
        mailer.send_pending(self.db, transport=FakeTransport())

        row = self.log_rows()[0]
        self.assertEqual(row["status"], "sent")
        self.assertIsNotNone(row["context"])

    def test_a_token_email_that_failed_keeps_its_context_to_retry(self):
        """Scrubbing on failure would guarantee the member never gets the
        link at all, which is the opposite of what the scrubbing is for."""
        mailer.enqueue(
            self.db,
            [
                mailer.EmailEvent(
                    kind="E9",
                    to_email="member@example.com",
                    context={
                        "full_name": "Chris V",
                        "reset_url": "https://example.test/reset?token=x",
                        "expires_hours": 2,
                    },
                )
            ],
        )
        transport = FakeTransport()
        transport.fail_next(1)
        mailer.send_pending(self.db, transport=transport)

        self.assertIsNotNone(self.log_rows()[0]["context"])


class AutomaticRetryTests(DurabilityTestBase):
    def test_a_failure_is_picked_up_again_with_nobody_intervening(self):
        mailer.enqueue(self.db, [self.booking_event()])
        transport = FakeTransport()
        transport.fail_next(1)

        first = mailer.send_pending(self.db, transport=transport)
        self.assertEqual(first.sent, 0)
        row = self.log_rows()[0]
        self.assertEqual(row["status"], "queued", "it gave up after one failure")
        self.assertEqual(int(row["attempts"]), 1)

        # The next flush -- a later request, or the reminder cron.
        second = mailer.send_pending(self.db, transport=transport)
        self.assertEqual(second.sent, 1)
        self.assertEqual(self.log_rows()[0]["status"], "sent")

    def test_how_many_attempts_to_make_is_an_admin_setting(self):
        self.set_setting("email_max_attempts", 1)
        mailer.enqueue(self.db, [self.booking_event()])
        transport = FakeTransport()
        transport.fail_next(5)

        mailer.send_pending(self.db, transport=transport)

        row = self.log_rows()[0]
        self.assertEqual(row["status"], "failed", "it kept trying past the budget")
        self.assertEqual(int(row["attempts"]), 1)

    def test_the_default_budget_is_the_three_the_spec_asks_for(self):
        settings = self.db.run_in_transaction(Settings.load)
        self.assertEqual(settings.email_max_attempts, 3)

    def set_setting(self, key: str, value) -> None:
        import json

        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?", (json.dumps(value), key)
            )
        )


class ImmediateDeliveryTests(DurabilityTestBase):
    """Mail used to wait for the cron, so up to fifteen minutes."""

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)

    def test_a_request_that_queues_mail_also_sends_it(self):
        self.client.get("/register")
        response = self.client.post(
            "/register",
            form={
                "email": "newcomer@example.com",
                "password": _PASSWORD,
                "confirm_password": _PASSWORD,
                "full_name": "New Comer",
                "department": "Operations",
                "phone": "0912345678",
            },
        )
        self.assertIn(response.status, (200, 303), "registration did not go through")

        row = self.query_one(
            "SELECT status FROM email_log WHERE to_email = ?",
            ("newcomer@example.com",),
        )
        self.assertIsNotNone(row, "no verification email was queued at all")
        self.assertEqual(
            row["status"], "sent", "the verification email is still waiting for cron"
        )

    def test_a_request_that_queues_nothing_does_no_delivery_work(self):
        self.client.get("/login")
        self.assertFalse(
            mailer.take_pending_hint(), "an idle request left the flush hint set"
        )


class ResendTests(DurabilityTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.create_user(
            email="admin@example.com", password=_PASSWORD, is_admin=True
        )
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)
        self.client.get("/login")
        self.client.post(
            "/login", form={"email": "admin@example.com", "password": _PASSWORD}
        )

    def test_a_delivered_booking_email_can_be_sent_again(self):
        """What the button is for: "I deleted it, please send it again"."""
        mailer.enqueue(self.db, [self.booking_event()])
        mailer.send_pending(self.db, transport=FakeTransport())
        row_id = self.log_rows()[0]["id"]

        response = self.client.post(f"/admin/emails/{row_id}/resend", form={})
        self.assertEqual(response.status, 303)
        self.assertIn("msg=email_resent", dict(response.headers)["Location"])

        row = self.query_one("SELECT status, attempts FROM email_log WHERE id = ?",
                             (row_id,))
        self.assertEqual(row["status"], "sent")

    def test_a_delivered_reset_link_cannot_and_says_why(self):
        mailer.enqueue(
            self.db,
            [
                mailer.EmailEvent(
                    kind="E9",
                    to_email="member@example.com",
                    context={
                        "full_name": "Chris V",
                        "reset_url": "https://example.test/reset?token=x",
                        "expires_hours": 2,
                    },
                )
            ],
        )
        mailer.send_pending(self.db, transport=FakeTransport())
        row_id = self.log_rows()[0]["id"]

        with self.assertRaises(ConflictError):
            mailer.resend(self.db, row_id)

        page = self.client.get("/admin/emails?err=EMAIL_NOT_RESENDABLE")
        self.assertNotIn("error.EMAIL_NOT_RESENDABLE", page.text,
                         "the refusal has no message")

    def test_the_button_is_shown_exactly_when_it_would_work(self):
        mailer.enqueue(self.db, [self.booking_event()])
        mailer.enqueue(
            self.db,
            [
                mailer.EmailEvent(
                    kind="E9",
                    to_email="member@example.com",
                    context={
                        "full_name": "Chris V",
                        "reset_url": "https://example.test/reset?token=x",
                        "expires_hours": 2,
                    },
                )
            ],
        )
        mailer.send_pending(self.db, transport=FakeTransport())
        rows = {r["type"]: r["id"] for r in self.log_rows()}

        page = self.client.get("/admin/emails")
        self.assertIn(f"/admin/emails/{rows['E4']}/resend", page.text)
        self.assertNotIn(f"/admin/emails/{rows['E9']}/resend", page.text)

    def test_an_unknown_row_is_reported_not_found(self):
        with self.assertRaises(NotFoundError):
            mailer.resend(self.db, models.new_id())

    def test_a_member_cannot_resend_anything(self):
        mailer.enqueue(self.db, [self.booking_event()])
        mailer.send_pending(self.db, transport=FakeTransport())
        row_id = self.log_rows()[0]["id"]

        member_client = Client(self.app)
        member_client.get("/login")
        member_client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )
        response = member_client.post(f"/admin/emails/{row_id}/resend", form={})
        self.assertIn(response.status, (303, 403), f"got {response.status}")
