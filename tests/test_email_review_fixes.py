"""Regression guards for two defects found in the independent review.

Both are the same shape: correct on SQLite, broken on Postgres. They are kept
in their own module because they belong to the review's findings rather than
to Task 2's original coverage.
"""

from __future__ import annotations

from app.services import mailer
from app.services.mailer import EmailEvent
from app.services.transports import FakeTransport
from app.timeutil import now_utc
from tests.support import AppTestCase, taipei_at


class EnqueueSavepointTests(AppTestCase):
    """A deduplicated event must not poison the rest of its batch.

    ``enqueue`` catches the unique-violation raised by a colliding
    ``dedupe_key`` and continues. On Postgres a failed statement aborts the
    whole transaction, so without a savepoint every *subsequent* insert in
    the batch would raise ``InFailedSqlTransaction``. SQLite is forgiving
    here, which is exactly why this needs an explicit test.
    """

    def _event(self, address: str, dedupe_key: str | None = None) -> EmailEvent:
        start = taipei_at(1, 14)
        return EmailEvent(
            kind="E10",
            to_email=address,
            context={
                "full_name": "測試使用者",
                "room_name": "會議室 A",
                "title": "週會",
                "start_at": start,
                "end_at": taipei_at(1, 15),
            },
            dedupe_key=dedupe_key,
        )

    def test_a_collision_does_not_prevent_later_events_in_the_batch(self):
        ids = mailer.enqueue(self.db, [self._event("a@example.com", "dup:1")])
        self.assertEqual(len([i for i in ids if i]), 1)

        batch = [
            self._event("a@example.com", "dup:1"),   # collides, must be dropped
            self._event("b@example.com", "dup:2"),
            self._event("c@example.com"),
        ]
        results = mailer.enqueue(self.db, batch)

        # enqueue returns the ids it actually created, so the dropped
        # duplicate simply does not appear. What matters is that the two
        # events *after* the collision were still written.
        self.assertEqual(len(results), 2)

        addresses = {
            row["to_email"] for row in self.query_all("SELECT to_email FROM email_log")
        }
        self.assertEqual(
            addresses, {"a@example.com", "b@example.com", "c@example.com"}
        )
        self.assertEqual(len(self.query_all("SELECT id FROM email_log")), 3)

    def test_the_collision_is_dropped_rather_than_duplicated(self):
        event = self._event("a@example.com", "only-once")
        mailer.enqueue(self.db, [event])
        mailer.enqueue(self.db, [event])
        rows = self.query_all(
            "SELECT id FROM email_log WHERE dedupe_key = ?", ("only-once",)
        )
        self.assertEqual(len(rows), 1)


class PreemptionNotificationStampTests(AppTestCase):
    """Spec §4.5's ``notification_sent_at`` must actually be populated."""

    def setUp(self) -> None:
        super().setUp()
        self.freeze_at(0, 9, 0)
        self.room = self.create_room()
        self.victim = self.create_user(level=2, email="victim@example.com")
        self.winner = self.create_user(level=8)
        self.booking = self.create_booking(
            room=self.room,
            user=self.victim,
            start_at=taipei_at(1, 14),
            end_at=taipei_at(1, 15),
        )

    def _log_a_preemption(self) -> None:
        from app.models import new_id

        def work(conn):
            conn.execute(
                "INSERT INTO preemption_log (id, victim_booking_id,"
                " winner_booking_id, victim_user_id, winner_user_id,"
                " victim_level, winner_level, room_id, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(), self.booking.id, self.booking.id,
                    self.victim.id, self.winner.id, 2, 8, self.room.id, now_utc(),
                ),
            )

        self.db.run_in_transaction(work)

    def test_it_is_stamped_only_once_the_notice_is_actually_sent(self):
        self._log_a_preemption()
        mailer.enqueue(
            self.db,
            [
                EmailEvent(
                    kind="E5",
                    to_email="victim@example.com",
                    context={
                        "full_name": "受害同事",
                        "reason": "preempted",
                        "room_name": self.room.name,
                        "title": "會議",
                        "start_at": self.booking.start_at,
                        "end_at": self.booking.end_at,
                    },
                    related_booking_id=self.booking.id,
                )
            ],
        )

        # Enqueuing is not notifying.
        row = self.query_one("SELECT notification_sent_at FROM preemption_log")
        self.assertIsNone(row["notification_sent_at"])

        mailer.send_pending(self.db, transport=FakeTransport())

        row = self.query_one("SELECT notification_sent_at FROM preemption_log")
        self.assertIsNotNone(row["notification_sent_at"])
