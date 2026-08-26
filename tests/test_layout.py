"""Layout guarantees that are easy to break and hard to notice.

Column alignment, jump targets and the mobile header are the kind of thing
that degrades silently: nothing errors, the page just becomes unpleasant.
These pin down the structural facts each one depends on.
"""

from __future__ import annotations

import re

from app.config import Config
from app.web.app import create_app
from app.web.layout import STYLESHEET
from tests.support import AppTestCase, taipei_at
from tests.webclient import Client

_PASSWORD = "a decent passphrase"


class LayoutTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)
        self.rooms = [
            self.create_room(name=name)
            for name in ("第一會議室", "第二會議室", "大型會議室", "小會議室")
        ]
        self.user = self.create_user(email="member@example.com", password=_PASSWORD)
        self.client.get("/login")
        self.client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )


class RoomJumpTests(LayoutTestBase):
    def test_every_room_in_the_list_has_a_matching_anchor(self):
        """A jump link pointing at nothing scrolls nowhere and looks broken."""
        html = self.client.get("/day").text
        targets = re.findall(r'href="#(room-[^"]+)"', html)
        anchors = re.findall(r'id="(room-[^"]+)"', html)

        self.assertEqual(len(targets), len(self.rooms))
        self.assertEqual(sorted(targets), sorted(anchors))

    def test_the_list_names_every_room(self):
        html = self.client.get("/day").text
        for room in self.rooms:
            with self.subTest(room=room.name):
                self.assertIn(room.name, html)

    def test_it_is_a_details_element_so_it_needs_no_javascript(self):
        html = self.client.get("/day").text
        self.assertIn('<details class="jump">', html)
        self.assertNotIn("<script", html)

    def test_no_jump_list_when_there_is_only_one_room(self):
        for room in self.rooms[1:]:
            self.db.run_in_transaction(
                lambda conn, rid=room.id: conn.execute(
                    "DELETE FROM rooms WHERE id = ?", (rid,)
                )
            )
        html = self.client.get("/day").text
        self.assertNotIn('<details class="jump">', html)

    def test_the_selected_room_is_marked_in_the_list(self):
        target = self.rooms[2]
        html = self.client.get(
            f"/day?room={target.id}&start=14%3A00"
        ).text
        self.assertIn('aria-current="true"', html)


class ColumnCountTests(LayoutTestBase):
    def test_the_day_grid_is_capped_at_three_columns(self):
        self.assertIn('class="day-grid cols-3"', self.client.get("/day").text)

    def test_the_week_grid_is_capped_at_four_columns(self):
        self.assertIn('class="day-grid cols-4"', self.client.get("/week").text)

    def test_the_stylesheet_caps_both_rather_than_auto_fitting(self):
        # auto-fit was what packed five rooms onto a wide screen. Only the
        # comment explaining that should mention it now, not a rule.
        self.assertNotIn("repeat(auto-fit", STYLESHEET)
        self.assertIn(".day-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }", STYLESHEET)
        self.assertIn(".day-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }", STYLESHEET)


class RowAlignmentTests(LayoutTestBase):
    def test_free_and_booked_slots_share_one_wrapper(self):
        """Same structure in both cases, or the rows differ in height."""
        self.create_booking(
            room=self.rooms[0],
            user=self.user,
            start_at=taipei_at(0, 14),
            end_at=taipei_at(0, 15),
            title="一個非常長的會議標題用來測試截斷行為是否正確",
        )
        html = self.client.get("/day").text

        wrappers = html.count('class="slot-detail"')
        rows = html.count('<li class="slot')
        self.assertEqual(
            wrappers, rows, "every slot row must carry exactly one detail wrapper"
        )

    def test_rows_have_a_fixed_height_in_the_stylesheet(self):
        # This is the whole mechanism: independent columns only line up if
        # every row is the same height regardless of its content.
        self.assertRegex(STYLESHEET, r"\.slot \{[^}]*height: 2\.75rem")
        self.assertRegex(STYLESHEET, r"\.slot-detail \{[^}]*text-overflow: ellipsis")

    def test_a_clipped_title_is_still_readable_via_the_title_attribute(self):
        long_title = "季度預算檢討會議與明年度計畫討論"
        self.create_booking(
            room=self.rooms[0],
            user=self.user,
            start_at=taipei_at(0, 14),
            end_at=taipei_at(0, 15),
            title=long_title,
        )
        html = self.client.get("/day").text
        self.assertIn(f'title="{long_title} ・ {self.user.full_name}"', html)


class WeekJumpTests(LayoutTestBase):
    def offered_weeks(self) -> list[str]:
        """Only the dates inside the jump list.

        The prev/next links in the date bar are also ``/week?...date=`` but
        step seven days from whatever is being shown, so they are deliberately
        not Monday-aligned and are not what this list is about.
        """
        html = self.client.get("/week").text
        block = re.search(r'<ul class="jump-list">(.*?)</ul>', html, re.S)
        assert block, "no jump list on the week view"
        return re.findall(r'date=(\d{4}-\d{2}-\d{2})', block.group(1))

    def test_it_offers_more_than_the_two_neighbouring_weeks(self):
        """The point of the list is not stepping one week at a time."""
        weeks = self.offered_weeks()
        self.assertGreater(len(weeks), 4, f"only {len(weeks)} weeks offered")

    def test_every_offered_week_starts_on_a_monday(self):
        from datetime import date

        for raw in self.offered_weeks():
            with self.subTest(week=raw):
                self.assertEqual(date.fromisoformat(raw).weekday(), 0)

    def test_the_current_week_is_labelled_and_marked(self):
        html = self.client.get("/week").text
        self.assertIn("本週", html)
        self.assertIn('aria-current="true"', html)

    def test_it_reaches_the_end_of_the_booking_horizon(self):
        from datetime import date, timedelta

        from app.timeutil import local_date, now_utc

        horizon = self.settings().booking_horizon_days
        furthest = max(date.fromisoformat(raw) for raw in self.offered_weeks())
        self.assertGreaterEqual(
            furthest + timedelta(days=6),
            local_date(now_utc()) + timedelta(days=horizon),
            "the list stops short of the last bookable week",
        )


class MobileHeaderTests(LayoutTestBase):
    def test_the_title_and_member_name_are_hidden_on_small_screens(self):
        mobile = re.search(
            r"@media \(max-width: 640px\) \{(.*?)\n\}", STYLESHEET, re.S
        )
        self.assertIsNotNone(mobile, "no small-screen block in the stylesheet")
        block = mobile.group(1)
        self.assertRegex(block, r"\.brand \{ display: none")
        self.assertRegex(block, r"\.account-name \{ display: none")

    def test_the_nav_scrolls_rather_than_wrapping(self):
        """Wrapping is what pushed the header past two rows."""
        mobile = re.search(
            r"@media \(max-width: 640px\) \{(.*?)\n\}", STYLESHEET, re.S
        ).group(1)
        self.assertIn("overflow-x: auto", mobile)
        self.assertIn("flex-wrap: nowrap", mobile)

    def test_the_markup_carries_the_hooks_those_rules_need(self):
        html = self.client.get("/day").text
        self.assertIn('class="brand"', html)
        self.assertIn('class="account-name"', html)

    def test_logout_survives_on_a_phone(self):
        """The name goes, the way out must not."""
        html = self.client.get("/day").text
        self.assertIn('action="/logout"', html)


class ScrollBehaviourTests(AppTestCase):
    def test_jumping_is_animated_but_respects_reduced_motion(self):
        self.assertIn("scroll-behavior: smooth", STYLESHEET)
        self.assertRegex(
            STYLESHEET,
            r"prefers-reduced-motion: reduce\) \{ html \{ scroll-behavior: auto",
        )

    def test_anchors_clear_the_sticky_header(self):
        # Without this the room heading lands underneath the header.
        self.assertRegex(STYLESHEET, r"\.room-column \{[^}]*scroll-margin-top")
