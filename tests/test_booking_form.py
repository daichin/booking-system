"""The booking form must be able to submit itself.

Every other UI test posts values a test author typed by hand. That is how a
form whose ``<select>`` sent minutes-past-midnight ("840") to a parser that
only understood "HH:MM" shipped with a green suite: both halves were tested,
never the join between them.

These tests scrape the rendered page and submit exactly what a browser would.
"""

from __future__ import annotations

import re
from datetime import timedelta

from app.config import Config
from app.models import CONFIRMED
from app.timeutil import local_date, now_utc, parse_hhmm
from app.web.app import create_app
from tests.support import AppTestCase
from tests.webclient import Client

_PASSWORD = "a decent passphrase"


def options_of(html: str, field_name: str) -> list[str]:
    """The ``value=""`` of every option inside one named select."""
    block = re.search(
        rf'<select[^>]*name="{field_name}"[^>]*>(.*?)</select>', html, re.S
    )
    assert block, f"no <select name=\"{field_name}\"> in the page"
    return re.findall(r'<option[^>]*value="([^"]*)"', block.group(1))


class BookingFormTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = Config(base_url="http://testserver", email_transport="fake")
        self.app = create_app(self.db, self.config)
        self.client = Client(self.app)

        self.room = self.create_room(name="會議室 A")
        self.user = self.create_user(email="member@example.com", password=_PASSWORD)

        self.client.get("/login")
        signed_in = self.client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )
        self.assertIn(signed_in.status, (200, 303), "login failed, cannot continue")

    def day_page(self) -> str:
        page = self.client.get("/day")
        self.assertEqual(page.status, 200)
        return page.text

    # --- the join between form and parser ---------------------------------

    def test_every_time_option_the_form_offers_is_one_the_parser_accepts(self):
        html = self.day_page()
        for name in ("start_time", "end_time"):
            values = options_of(html, name)
            self.assertTrue(values, f"{name} offered no options")
            for value in values:
                with self.subTest(field=name, value=value):
                    # Must not raise. This is the exact check the form's
                    # submitted value goes through server-side.
                    parse_hhmm(value)

    def test_submitting_the_form_as_rendered_creates_a_booking(self):
        html = self.day_page()
        starts = options_of(html, "start_time")
        rooms = options_of(html, "room_id")

        # 14:00 -> 15:00, chosen from what the page actually offers.
        start = "14:00"
        end = "15:00"
        self.assertIn(start, starts)
        self.assertIn(end, options_of(html, "end_time"))

        tomorrow = local_date(now_utc()) + timedelta(days=1)

        response = self.client.post(
            "/bookings",
            form={
                "room_id": rooms[0],
                "date": tomorrow.isoformat(),
                "start_time": start,
                "end_time": end,
                "title": "週會",
            },
        )

        self.assertNotEqual(
            response.status, 400, f"the form rejected its own values: {response.text[:400]}"
        )
        booked = self.query_all(
            "SELECT * FROM bookings WHERE status = ?", (CONFIRMED,)
        )
        self.assertEqual(len(booked), 1)

    # --- the error message must name the field that is actually wrong -----

    def test_a_bad_time_is_not_reported_as_a_missing_date(self):
        tomorrow = local_date(now_utc()) + timedelta(days=1)
        response = self.client.post(
            "/bookings",
            form={
                "room_id": self.room.id,
                "date": tomorrow.isoformat(),
                "start_time": "840",     # the old, wrong encoding
                "end_time": "15:00",
                "title": "週會",
            },
        )
        self.assertEqual(response.status, 400)
        # It must blame the time field, not the date the member filled in
        # correctly. "日期" appearing here is the bug this test exists for.
        self.assertIn("開始時間", response.text)

    def test_a_genuinely_missing_date_still_names_the_date(self):
        response = self.client.post(
            "/bookings",
            form={
                "room_id": self.room.id,
                "date": "",
                "start_time": "14:00",
                "end_time": "15:00",
                "title": "週會",
            },
        )
        self.assertEqual(response.status, 400)
        self.assertIn("日期", response.text)

    def test_a_refused_request_is_logged_with_its_reason(self):
        """The log line is the whole point: a bare 400 in an access log tells
        an operator nothing, and that is what made this bug slow to find."""
        with self.assertLogs("app.web", level="WARNING") as captured:
            self.client.post(
                "/bookings",
                form={
                    "room_id": self.room.id,
                    "date": "",
                    "start_time": "14:00",
                    "end_time": "15:00",
                    "title": "週會",
                },
            )
        line = "\n".join(captured.output)
        self.assertIn("POST /bookings", line)
        self.assertIn("400", line)
        self.assertIn("MISSING_FIELD", line)
        self.assertIn("date", line)

    def test_the_log_does_not_leak_personal_data(self):
        with self.assertLogs("app.web", level="INFO") as captured:
            self.client.get("/day")
        line = "\n".join(captured.output)
        self.assertNotIn("member@example.com", line)

    def test_field_names_are_shown_in_chinese_not_as_column_names(self):
        tomorrow = local_date(now_utc()) + timedelta(days=1)
        response = self.client.post(
            "/bookings",
            form={
                "room_id": self.room.id,
                "date": tomorrow.isoformat(),
                "start_time": "",
                "end_time": "15:00",
                "title": "週會",
            },
        )
        self.assertNotIn("start_time", response.text)
