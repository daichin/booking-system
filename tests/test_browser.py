"""Tests that need a real browser.

Everything else in this suite asserts on HTML and on stylesheet text, which
can only show that a rule was written -- not that it produced the effect it
was written for. The back-to-top button was the proof: three tests passed
while the button did nothing, because its target was a sticky header that is
always already at the top of the viewport. Nothing short of a real layout
engine could have caught that.

These run against a live server with Chromium. They are skipped when
Playwright is not installed, so the SQLite and Postgres CI jobs are
unaffected; a separate job installs it and runs them.
"""

from __future__ import annotations

import threading
import time
import unittest
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from app.config import Config
from app.web.app import create_app
from tests.support import AppTestCase, taipei_at

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the other CI jobs take this path
    sync_playwright = None

_PASSWORD = "a decent passphrase"


class _QuietServer(ThreadingMixIn, WSGIServer):
    """Threaded so the browser's parallel requests do not deadlock."""

    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):  # noqa: D102 - silence per-request logging
        pass


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class BrowserTestCase(AppTestCase):
    """Serves the app on a loopback port and drives Chromium against it."""

    viewport = {"width": 1400, "height": 900}

    def setUp(self) -> None:
        super().setUp()
        self.rooms = [
            self.create_room(name=name)
            for name in ("第一會議室", "第二會議室", "大型會議室", "小會議室")
        ]
        self.user = self.create_user(email="member@example.com", password=_PASSWORD)

        app = create_app(
            self.db, Config(base_url="http://127.0.0.1", email_transport="fake")
        )
        self.httpd = make_server(
            "127.0.0.1", 0, app, server_class=_QuietServer, handler_class=_QuietHandler
        )
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

        self._playwright = sync_playwright().start()
        self.addCleanup(self._playwright.stop)
        self.browser = self._playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport=self.viewport)

    def login(self) -> None:
        self.page.goto(f"{self.base}/login")
        self.page.fill('input[name="email"]', "member@example.com")
        self.page.fill('input[name="password"]', _PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/day**")

    def scroll_y(self) -> float:
        return self.page.evaluate("window.pageYOffset")

    def wait_until(self, expression: str, timeout: float = 5.0) -> None:
        """Poll from Python instead of Playwright's wait_for_function.

        That helper evaluates a string inside the page, which our own
        Content-Security-Policy refuses (no 'unsafe-eval') -- a strict policy
        working as intended. page.evaluate goes through the debugger protocol
        and is unaffected.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.page.evaluate(expression):
                return
            self.page.wait_for_timeout(50)
        raise AssertionError(f"condition never became true: {expression}")


class BackToTopBrowserTests(BrowserTestCase):
    def test_clicking_it_actually_returns_to_the_top(self):
        """The regression that started all this."""
        self.login()
        self.page.evaluate("window.scrollTo(0, 1200)")
        self.wait_until("window.pageYOffset > 500")

        self.page.click(".to-top")
        self.wait_until("window.pageYOffset === 0")
        self.assertEqual(self.scroll_y(), 0)

    def test_it_is_out_of_the_way_until_there_is_something_to_go_back_to(self):
        self.login()
        self.assertFalse(
            self.page.is_visible(".to-top:not(.is-hidden)"),
            "the button should stay out of sight at the top of the page",
        )
        self.page.evaluate("window.scrollTo(0, 1200)")
        self.page.wait_for_selector(".to-top:not(.is-hidden)", timeout=5000)

    def test_it_does_not_cover_the_content_on_a_phone(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.login()
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.page.wait_for_selector(".to-top:not(.is-hidden)", timeout=5000)

        button = self.page.locator(".to-top").bounding_box()
        footer = self.page.locator(".site-footer").bounding_box()
        self.assertIsNotNone(button)
        # The bottom padding must leave the last content clear of the button.
        self.assertGreater(button["y"], footer["y"] - button["height"] * 3)


class SlotPickingBrowserTests(BrowserTestCase):
    def test_picking_a_start_keeps_your_scroll_position(self):
        """The whole point of the change: no jump back to the top."""
        self.login()
        self.page.evaluate("window.scrollTo(0, 900)")
        self.wait_until("window.pageYOffset > 500")
        before = self.scroll_y()

        self.page.locator("a.slot-action").nth(20).click()
        self.page.wait_for_selector("li.slot.is-start", timeout=5000)

        after = self.scroll_y()
        self.assertLess(
            abs(after - before), 120, f"scrolled from {before} to {after}"
        )

    def test_the_chosen_row_is_marked_and_end_options_appear(self):
        self.login()
        self.page.locator("a.slot-action").nth(6).click()
        self.page.wait_for_selector("li.slot.is-start", timeout=5000)

        self.assertEqual(self.page.locator("li.slot.is-start").count(), 1)
        self.assertTrue(self.page.is_visible(".selection-banner"))
        self.assertGreater(self.page.locator('a[href^="/book?"]').count(), 0)

    def test_it_swapped_in_place_rather_than_reloading(self):
        self.login()
        self.page.locator("a.slot-action").nth(6).click()
        self.page.wait_for_selector("li.slot.is-start", timeout=5000)
        self.assertEqual(
            self.page.get_attribute("html", "data-swapped"),
            "true",
            "the grid should have been swapped in place",
        )
        self.assertIn("start=", self.page.url)


class LayoutBrowserTests(BrowserTestCase):
    def test_slot_rows_are_all_the_same_height(self):
        """What keeps the times lining up across columns."""
        self.create_booking(
            room=self.rooms[0],
            user=self.user,
            start_at=taipei_at(0, 14),
            end_at=taipei_at(0, 15),
            title="一個很長的會議標題會被截斷但不應該把這一列撐高",
        )
        self.login()
        heights = self.page.eval_on_selector_all(
            "li.slot",
            "rows => rows.map(r => Math.round(r.getBoundingClientRect().height))",
        )
        self.assertGreater(len(heights), 20)
        self.assertEqual(
            len(set(heights)), 1, f"slot rows have differing heights: {set(heights)}"
        )

    def test_the_first_row_of_each_column_lines_up(self):
        self.create_booking(
            room=self.rooms[1],
            user=self.user,
            start_at=taipei_at(0, 9),
            end_at=taipei_at(0, 10),
            title="長標題長標題長標題長標題長標題",
        )
        self.login()
        tops = self.page.eval_on_selector_all(
            ".room-column .slot-list li.slot:first-child",
            "rows => rows.map(r => Math.round(r.getBoundingClientRect().top))",
        )
        self.assertGreater(len(tops), 1)
        # Only columns on the same row of the grid should line up; with four
        # rooms and a cap of three, the fourth is legitimately lower down.
        first_row = [top for top in tops if top == min(tops)]
        self.assertGreaterEqual(len(first_row), 2, f"tops were {tops}")
        self.assertEqual(
            len(set(first_row)), 1, f"columns start at different heights: {tops}"
        )

    def test_a_wide_screen_shows_at_most_three_rooms_across(self):
        self.login()
        tops = self.page.eval_on_selector_all(
            ".room-column",
            "cols => cols.map(c => Math.round(c.getBoundingClientRect().top))",
        )
        first_row = [top for top in tops if top == min(tops)]
        self.assertLessEqual(len(first_row), 3, f"{len(first_row)} rooms on one row")

    def test_a_phone_stacks_the_rooms(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.login()
        lefts = self.page.eval_on_selector_all(
            ".room-column",
            "cols => cols.map(c => Math.round(c.getBoundingClientRect().left))",
        )
        self.assertEqual(len(set(lefts)), 1, "rooms should be stacked, not side by side")


class MobileHeaderBrowserTests(BrowserTestCase):
    viewport = {"width": 390, "height": 800}

    def test_the_header_is_at_most_two_rows_tall(self):
        self.login()
        box = self.page.locator(".site-header").bounding_box()
        # Two rows of nav-sized controls plus padding.
        self.assertLess(box["height"], 120, f"header is {box['height']}px tall")

    def test_the_title_and_name_are_actually_hidden(self):
        self.login()
        self.assertFalse(self.page.is_visible(".brand"))
        self.assertFalse(self.page.is_visible(".account-name"))

    def test_logging_out_is_still_reachable(self):
        self.login()
        self.assertTrue(self.page.is_visible('form[action="/logout"] button'))

    def test_the_nav_does_not_wrap(self):
        self.login()
        tops = self.page.eval_on_selector_all(
            ".site-nav a",
            "links => links.map(l => Math.round(l.getBoundingClientRect().top))",
        )
        self.assertEqual(len(set(tops)), 1, "nav links wrapped onto several rows")


class JumpDropdownBrowserTests(BrowserTestCase):
    def test_the_list_becomes_a_real_dropdown(self):
        self.login()
        self.page.wait_for_selector("select.jump-select", timeout=5000)
        self.assertEqual(self.page.locator("details.jump").count(), 0)

    def test_choosing_a_room_scrolls_to_it(self):
        self.login()
        select = self.page.locator("select.jump-select").first
        select.wait_for()
        last_room = self.rooms[-1]
        select.select_option(f"#room-{last_room.id}")

        self.wait_until("window.pageYOffset > 0")
        box = self.page.locator(f"#room-{last_room.id}").bounding_box()
        self.assertGreaterEqual(box["y"], 0)
        self.assertLess(box["y"], self.viewport["height"])
