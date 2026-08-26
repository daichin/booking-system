"""The two-click booking flow, and the language switch.

The flow is exercised the way a browser would: read the page, follow the
links it actually rendered, submit the buttons it actually drew. Nothing here
constructs a URL by hand, so a link that stops being generated fails a test
rather than quietly disappearing from the UI.
"""

from __future__ import annotations

import re
from datetime import timedelta

from app.config import Config
from app.models import CONFIRMED
from app.settings import Settings
from app.timeutil import local_date, now_utc
from app.web.app import create_app
from tests.support import AppTestCase, taipei_at
from tests.webclient import Client

_PASSWORD = "a decent passphrase"

_HREFS = re.compile(r'href="([^"]*)"')


def links_matching(html: str, needle: str) -> list[str]:
    return [href for href in _HREFS.findall(html) if needle in href]


def unescape(href: str) -> str:
    return href.replace("&amp;", "&")


class BookingFlowTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)
        self.room = self.create_room(name="會議室 A")
        self.user = self.create_user(email="member@example.com", password=_PASSWORD)
        self.client.get("/login")
        self.client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )
        self.tomorrow = local_date(now_utc()) + timedelta(days=1)

    def day(self, **params) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = self.client.get(f"/day?date={self.tomorrow}&{query}" if query
                                   else f"/day?date={self.tomorrow}")
        self.assertEqual(response.status, 200)
        return response.text


class SlotPickingTests(BookingFlowTestBase):
    def test_slots_offer_a_start_link_before_anything_is_picked(self):
        html = self.day()
        starts = links_matching(html, "start=")
        self.assertTrue(starts, "no slot offered a start link")
        self.assertIn("room=", starts[0])

    def test_picking_a_start_switches_the_page_into_selecting_mode(self):
        first_start = unescape(links_matching(self.day(), "start=")[0])
        picked = self.client.get(first_start)
        self.assertEqual(picked.status, 200)

        # The banner tells the member what the second click will mean.
        self.assertIn('class="selection-banner"', picked.text)
        self.assertIn('class="slot is-start"', picked.text)
        # And now the slots offer ends rather than starts.
        self.assertTrue(links_matching(picked.text, "/book?"))

    def test_the_second_click_carries_both_times_to_the_confirm_page(self):
        first_start = unescape(links_matching(self.day(), "start=")[0])
        picked = self.client.get(first_start)
        end_link = unescape(links_matching(picked.text, "/book?")[0])

        self.assertIn("room_id=", end_link)
        self.assertIn("start_time=", end_link)
        self.assertIn("end_time=", end_link)

        confirm = self.client.get(end_link)
        self.assertEqual(confirm.status, 200)
        self.assertIn("會議室 A", confirm.text)

    def test_the_start_slot_itself_offers_a_single_slot_booking(self):
        first_start = unescape(links_matching(self.day(), "start=")[0])
        picked = self.client.get(first_start)
        # The row that is the start must still be clickable, otherwise
        # booking exactly one slot would be impossible.
        self.assertIn('class="slot is-start"', picked.text)
        starts_at = re.search(r"start=(\d\d%3A\d\d|\d\d:\d\d)", first_start).group(1)
        self.assertTrue(links_matching(picked.text, "/book?"))
        self.assertIn(starts_at.replace("%3A", ":"), unescape(picked.text))

    def test_selection_can_be_cancelled(self):
        first_start = unescape(links_matching(self.day(), "start=")[0])
        picked = self.client.get(first_start)
        cancels = [h for h in links_matching(picked.text, "/day?") if "start=" not in h]
        self.assertTrue(cancels)
        cleared = self.client.get(unescape(cancels[0]))
        self.assertNotIn('class="selection-banner"', cleared.text)

    def test_end_options_stop_at_the_maximum_duration(self):
        self.set_setting("max_booking_minutes", 60)
        first_start = unescape(links_matching(self.day(), "start=")[0])
        picked = self.client.get(first_start)
        # 30-minute slots, 60-minute cap: the start slot plus one more.
        self.assertEqual(len(links_matching(picked.text, "/book?")), 2)

    def test_an_occupied_slot_is_still_clickable(self):
        """Chosen behaviour: the engine decides, not the grid."""
        holder = self.create_user(level=9)
        self.create_booking(
            room=self.room,
            user=holder,
            start_at=taipei_at(1, 14),
            end_at=taipei_at(1, 15),
            title="別人的會議",
        )
        html = self.day()
        self.assertIn("別人的會議", html)
        self.assertTrue(links_matching(html, "start="), "occupied slots lost their link")


class ConfirmPageTests(BookingFlowTestBase):
    def confirm_page(self, start="14:00", end="15:00") -> str:
        response = self.client.get(
            f"/book?room_id={self.room.id}&date={self.tomorrow}"
            f"&start_time={start}&end_time={end}"
        )
        self.assertEqual(response.status, 200)
        return response.text

    def test_it_offers_the_configured_subject_presets_as_buttons(self):
        presets = self.db.run_in_transaction(Settings.load).title_presets
        html = self.confirm_page()
        for name in presets:
            with self.subTest(preset=name):
                self.assertIn(f'value="{name}"', html)
        self.assertIn('name="title"', html)

    def test_one_click_on_a_preset_completes_the_booking(self):
        presets = self.db.run_in_transaction(Settings.load).title_presets
        chosen = presets[0]

        response = self.client.post(
            "/bookings",
            form={
                "room_id": self.room.id,
                "date": self.tomorrow.isoformat(),
                "start_time": "14:00",
                "end_time": "15:00",
                "title": chosen,
            },
        )
        self.assertNotEqual(response.status, 400, response.text[:300])
        rows = self.query_all("SELECT title FROM bookings WHERE status = ?", (CONFIRMED,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], chosen)

    def test_a_typed_subject_beats_the_preset_buttons(self):
        self.client.post(
            "/bookings",
            form={
                "room_id": self.room.id,
                "date": self.tomorrow.isoformat(),
                "start_time": "14:00",
                "end_time": "15:00",
                "title": "週會",
                "custom_title": "Q3 預算討論",
            },
        )
        rows = self.query_all("SELECT title FROM bookings WHERE status = ?", (CONFIRMED,))
        self.assertEqual(rows[0]["title"], "Q3 預算討論")

    def test_recently_used_subjects_are_offered(self):
        self.create_booking(
            room=self.room,
            user=self.user,
            start_at=taipei_at(-5, 14),
            end_at=taipei_at(-5, 15),
            title="歷史專案會議",
        )
        self.assertIn("歷史專案會議", self.confirm_page())

    def test_it_warns_before_overriding_someone(self):
        junior = self.create_user(level=1)
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET level = ? WHERE id = ?", (7, self.user.id)
            )
        )
        self.create_booking(
            room=self.room,
            user=junior,
            start_at=taipei_at(1, 14),
            end_at=taipei_at(1, 15),
            title="低階會議",
        )
        html = self.confirm_page()
        self.assertIn('class="confirm-panel"', html)
        self.assertIn('name="confirm_preemption"', html)

    def test_a_blocked_slot_offers_no_submit_button(self):
        senior = self.create_user(level=10)
        self.create_booking(
            room=self.room,
            user=senior,
            start_at=taipei_at(1, 14),
            end_at=taipei_at(1, 15),
        )
        html = self.confirm_page()
        self.assertNotIn('action="/bookings"', html)


class DateNavigationTests(BookingFlowTestBase):
    def test_the_date_picker_is_a_native_date_input(self):
        html = self.day()
        self.assertIn('type="date"', html)
        self.assertIn('action="/day"', html)

    def test_prev_today_and_next_all_render(self):
        html = self.day()
        self.assertTrue(links_matching(html, (self.tomorrow - timedelta(days=1)).isoformat()))
        self.assertTrue(links_matching(html, (self.tomorrow + timedelta(days=1)).isoformat()))
        self.assertTrue(links_matching(html, local_date(now_utc()).isoformat()))

    def test_jumping_to_an_arbitrary_date_works(self):
        target = local_date(now_utc()) + timedelta(days=30)
        response = self.client.get(f"/day?date={target}")
        self.assertEqual(response.status, 200)
        self.assertTrue(links_matching(response.text, target.isoformat()))


class LanguageTests(BookingFlowTestBase):
    def test_the_switcher_offers_the_other_language(self):
        html = self.day()
        self.assertTrue(links_matching(html, "lang=en"))

    def test_switching_to_english_translates_the_page(self):
        response = self.client.get("/day?lang=en")
        self.assertEqual(response.status, 200)
        self.assertIn("My bookings", response.text)
        self.assertNotIn("我的預約", response.text)

    def test_the_choice_sticks_for_later_requests(self):
        self.client.get("/day?lang=en")
        later = self.client.get("/day")
        self.assertIn("My bookings", later.text)

    def test_the_choice_is_saved_to_the_member_so_email_can_use_it(self):
        self.client.get("/day?lang=en")
        row = self.query_one("SELECT locale FROM users WHERE id = ?", (self.user.id,))
        self.assertEqual(row["locale"], "en")

    def test_switching_back_to_chinese_works(self):
        self.client.get("/day?lang=en")
        back = self.client.get("/day?lang=zh-TW")
        self.assertIn("我的預約", back.text)

    def test_an_english_speaker_gets_english_before_choosing(self):
        fresh = Client(self.app)
        response = fresh.get(
            "/login", headers={"HTTP_ACCEPT_LANGUAGE": "en-GB,en;q=0.9"}
        )
        self.assertIn("Log in", response.text)

    def test_dates_are_rendered_in_the_readers_language(self):
        english = self.client.get("/day?lang=en").text
        self.assertNotIn("(台北時間)", english)
        # An English weekday abbreviation must appear in the day heading.
        self.assertRegex(english, r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} \w{3} \d{4}")
