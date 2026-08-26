"""Mail is written in the recipient's language, not the sender's.

The reminder job runs from cron with no browser and no request, so the only
way it can know a member reads English is the preference stored against that
member. These tests pin that down.
"""

from __future__ import annotations

from app import i18n
from app.services import mailer
from app.services.mailer import EmailEvent
from app.services.transports import FakeTransport
from tests.support import AppTestCase, taipei_at


class EmailLocaleTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.freeze_at(0, 9, 0)
        self.room = self.create_room(name="會議室 A")
        self.zh_user = self.create_user(email="zh@example.com")
        self.en_user = self.create_user(email="en@example.com")
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET locale = ? WHERE id = ?", ("en", self.en_user.id)
            )
        )

    def _event(self, address: str) -> EmailEvent:
        return EmailEvent(
            kind="E4",
            to_email=address,
            context={
                "full_name": "Test User",
                "room_name": self.room.name,
                "title": "Weekly sync",
                "start_at": taipei_at(1, 14),
                "end_at": taipei_at(1, 15),
                "booking_id": "x",
            },
        )

    def _send_to(self, address: str) -> str:
        transport = FakeTransport()
        mailer.enqueue(self.db, [self._event(address)])
        mailer.send_pending(self.db, transport=transport)
        self.assertTrue(transport.sent, "nothing was sent")
        message = transport.sent[-1]
        return f"{message.subject}\n{message.text}"

    def test_an_english_member_receives_english(self):
        body = self._send_to("en@example.com")
        self.assertIn("Taipei time", body)
        self.assertNotIn("台北時間", body)

    def test_a_chinese_member_still_receives_chinese(self):
        body = self._send_to("zh@example.com")
        self.assertIn("台北時間", body)

    def test_the_language_does_not_leak_between_recipients(self):
        """Two members, two languages, one batch."""
        transport = FakeTransport()
        mailer.enqueue(
            self.db,
            [self._event("en@example.com"), self._event("zh@example.com")],
        )
        mailer.send_pending(self.db, transport=transport)

        by_address = {m.to_email: f"{m.subject}\n{m.text}" for m in transport.sent}
        self.assertIn("Taipei time", by_address["en@example.com"])
        self.assertIn("台北時間", by_address["zh@example.com"])

    def test_rendering_restores_the_ambient_locale(self):
        """A request being handled must not be left in the recipient's language."""
        i18n.set_locale("zh-TW")
        self._send_to("en@example.com")
        self.assertEqual(i18n.current_locale(), "zh-TW")

    def test_an_unknown_address_falls_back_to_the_default(self):
        body = self._send_to("nobody@example.com")
        self.assertIn("台北時間", body)
