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


class InPlaceNavigationBrowserTests(BrowserTestCase):
    """Moving around the grid must not move the page under you.

    Cancelling a selection used to reload the page and then jump to the slot
    fragment, shifting the view by a few hundred pixels for no reason the
    member could see.
    """

    def select_a_slot(self, index: int = 18, scroll_to: int = 700) -> float:
        self.login()
        self.page.evaluate(f"window.scrollTo(0, {scroll_to})")
        self.wait_until(f"window.pageYOffset >= {min(scroll_to, 400)}")
        self.page.locator("a.slot-action").nth(index).click()
        self.page.wait_for_selector("li.slot.is-start", timeout=5000)
        return self.scroll_y()

    def test_cancelling_a_selection_does_not_move_the_page(self):
        before = self.select_a_slot()
        self.page.locator(".selection-banner a").first.click()
        self.page.wait_for_selector("li.slot.is-start", state="detached", timeout=5000)

        after = self.scroll_y()
        self.assertLess(
            abs(after - before), 60, f"cancelling moved the page {before} -> {after}"
        )

    def test_cancelling_swaps_in_place_rather_than_reloading(self):
        self.select_a_slot()
        self.page.evaluate("document.documentElement.removeAttribute('data-swapped')")
        self.page.locator(".selection-banner a").first.click()
        self.page.wait_for_selector("li.slot.is-start", state="detached", timeout=5000)
        self.assertEqual(self.page.get_attribute("html", "data-swapped"), "true")

    def test_changing_day_keeps_your_place(self):
        self.login()
        self.page.evaluate("window.scrollTo(0, 700)")
        self.wait_until("window.pageYOffset >= 400")
        before = self.scroll_y()

        # Click through the DOM rather than Playwright's click, which scrolls
        # the target into view first -- the date bar is at the top of the
        # page, so that would move us to 0 before the click even happened and
        # the test would be measuring its own setup.
        self.page.eval_on_selector(
            '.date-bar a[href^="/day?"]:last-of-type', "el => el.click()"
        )
        self.wait_until("document.documentElement.dataset.swapped === 'true'")

        after = self.scroll_y()
        self.assertLess(
            abs(after - before), 60, f"changing day moved the page {before} -> {after}"
        )

    def test_the_date_picker_also_swaps_in_place(self):
        from datetime import timedelta

        from app.timeutil import local_date, now_utc

        self.login()
        target = (local_date(now_utc()) + timedelta(days=5)).isoformat()
        self.page.fill('.date-jump input[name="date"]', target)
        self.page.click('.date-jump button[type="submit"]')
        self.wait_until("document.documentElement.dataset.swapped === 'true'")
        self.assertIn(target, self.page.url)

    def test_crossing_to_another_page_updates_the_nav_marker(self):
        """The nav sits outside the swapped region, so it needs updating too."""
        self.login()
        self.page.goto(f"{self.base}/week")
        self.page.wait_for_selector("a.slot-action", timeout=5000)

        self.page.locator("a.slot-action").first.click()
        self.wait_until("document.documentElement.dataset.swapped === 'true'")

        current = self.page.eval_on_selector_all(
            '.site-nav a[aria-current="page"]',
            "links => links.map(l => l.getAttribute('href'))",
        )
        self.assertEqual(current, ["/day"], f"nav still marks {current}")

    def test_the_page_title_follows_too(self):
        self.login()
        self.page.goto(f"{self.base}/week")
        self.page.wait_for_selector("a.slot-action", timeout=5000)
        before = self.page.title()

        self.page.locator("a.slot-action").first.click()
        self.wait_until("document.documentElement.dataset.swapped === 'true'")
        self.assertNotEqual(self.page.title(), before)

    def test_nav_links_themselves_still_navigate_normally(self):
        """They are plain /day and /week, and must not be intercepted."""
        self.login()
        self.page.click('.site-nav a[href="/week"]')
        self.page.wait_for_url("**/week")
        self.assertTrue(self.page.url.endswith("/week"))


class AdminLevelChangeBrowserTests(BrowserTestCase):
    """Changing a member's level, through a real form submission.

    The in-process client posts a dict, so it can only ever send one value per
    name -- which is exactly why it could not reproduce this. A browser sends
    both fields when two share a name, and the empty one won.
    """

    def setUp(self) -> None:
        super().setUp()
        self.admin = self.create_user(
            email="admin@example.com", password=_PASSWORD, is_admin=True, level=10
        )

    def login_as_admin(self) -> None:
        self.page.goto(f"{self.base}/login")
        self.page.fill('input[name="email"]', "admin@example.com")
        self.page.fill('input[name="password"]', _PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/day**")

    def test_the_form_does_not_submit_two_fields_of_the_same_name(self):
        self.login_as_admin()
        self.page.goto(f"{self.base}/admin/members")
        submitted = self.page.evaluate(
            """() => {
                var form = document.querySelector('form[action$="/level"]');
                return Array.from(new FormData(form).keys());
            }"""
        )
        self.assertEqual(
            len(submitted), len(set(submitted)), f"duplicate field names: {submitted}"
        )

    def test_choosing_a_level_actually_changes_it(self):
        self.login_as_admin()
        self.page.goto(f"{self.base}/admin/members")

        # Target this member's row explicitly: the list is sorted, so .first
        # is whichever account happens to sort earliest -- the test would
        # otherwise change the admin's level and report the member unchanged.
        row = self.page.locator(
            f'form[action="/admin/members/{self.user.id}/level"]'
        )
        row.locator("select").select_option("7")
        row.locator('button[type="submit"]').click()
        self.page.wait_for_load_state()

        self.assertNotIn("err=", self.page.url, "the level change was refused")
        levels = self.query_all(
            "SELECT level FROM users WHERE id = ?", (self.user.id,)
        )
        self.assertEqual(int(levels[0]["level"]), 7)


#: Measures the page the way a reader experiences it. Returns the page's own
#: horizontal overflow, how many rows the header actually occupies, and every
#: element painting content wider than the box it sits in.
_LAYOUT_PROBE = """
() => {
  const out = {overflow: document.documentElement.scrollWidth - window.innerWidth,
               headerRows: 0, offenders: []};

  const bar = document.querySelector('.site-header .bar');
  if (bar) {
    // Rows are counted by vertical overlap, not by an identical `top`: two
    // controls of different heights share a row with different tops, which
    // reported three rows for a header that plainly had two.
    const boxes = Array.from(bar.children)
      .map(c => c.getBoundingClientRect())
      .filter(r => r.width > 0 && r.height > 0)
      .sort((a, b) => a.top - b.top);
    const rows = [];
    for (const r of boxes) {
      const row = rows.find(x => r.top < x.bottom - 2 && r.bottom > x.top + 2);
      if (row) {
        row.top = Math.min(row.top, r.top);
        row.bottom = Math.max(row.bottom, r.bottom);
      } else {
        rows.push({top: r.top, bottom: r.bottom});
      }
    }
    out.headerRows = rows.length;
  }

  // Wide tables, the mobile nav and the jump lists scroll on purpose.
  const SCROLLERS = ['.table-wrap', '.site-nav', '.jump-list', '.slot-list'];
  for (const el of document.querySelectorAll('body *')) {
    if (SCROLLERS.some(s => el.matches(s) || el.closest(s))) continue;
    const cs = getComputedStyle(el);
    if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') continue;
    if (cs.display === 'none' || cs.position === 'absolute') continue;
    if (el.clientWidth <= 0) continue;
    // An element told to ellipsise is not being crowded out when it does --
    // that is the affordance working, and for arbitrary-length content like a
    // member's name it is the only correct behaviour. Counting it as a defect
    // also made this check font-dependent: the same name fits on a machine
    // with the CJK stack installed and overflows on a CI runner without it,
    // so the audit passed locally and failed in CI over nothing.
    if (cs.textOverflow === 'ellipsis'
        && (cs.overflow === 'hidden' || cs.overflow === 'clip')) continue;
    const over = el.scrollWidth - el.clientWidth;
    if (over > 1) {
      out.offenders.push({
        what: el.tagName.toLowerCase() + '.' + (el.className || '').toString().trim(),
        over: over,
        text: (el.textContent || '').trim().slice(0, 50),
      });
    }
  }
  return out;
}
"""

#: Phone, large phone, tablet, small laptop, desktop.
_WIDTHS = [(360, 740), (390, 844), (768, 1024), (1024, 768), (1400, 900)]

_AUDITED_PAGES = [
    "/day", "/week", "/my", "/account",
    "/admin/members", "/admin/rooms", "/admin/settings", "/admin/emails",
]


class ResponsiveLayoutTests(BrowserTestCase):
    """Nothing may be crowded out, in either language, at any width.

    English strings are typically two to three times the width of their
    zh-TW equivalents, so a header or a form row that fits in Chinese can
    still shove a control off the edge in English. Two defects were found
    exactly this way and neither was visible in the markup: a checkbox
    inheriting `width: 100%` from the shared input rule, which stretched
    into a full-width bar and pushed its own label out; and a member's name
    capped at 8em, which truncated an ordinary English name on a 1400px
    screen. Both are asserted here rather than described in a comment.
    """

    #: A name long enough to crowd the header if nothing holds it back.
    admin_name = "Alexandra Featherstonehaugh"

    def setUp(self) -> None:
        super().setUp()
        self.admin = self.create_user(
            email="admin@example.com",
            password=_PASSWORD,
            is_admin=True,
            full_name=self.admin_name,
            department="Engineering",
        )

    def _audit(self, locale: str) -> None:
        self.page.goto(f"{self.base}/login")
        self.page.fill('input[name="email"]', "admin@example.com")
        self.page.fill('input[name="password"]', _PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/day**")
        # After logging in, so the choice is stored against this account
        # rather than overwritten by the account's own saved language.
        self.page.goto(f"{self.base}/day?lang={locale}")

        for width, height in _WIDTHS:
            self.page.set_viewport_size({"width": width, "height": height})
            for path in _AUDITED_PAGES:
                self.page.goto(f"{self.base}{path}")
                result = self.page.evaluate(_LAYOUT_PROBE)
                where = f"{locale} {width}px {path}"

                with self.subTest(page=where, check="page overflow"):
                    self.assertLessEqual(
                        result["overflow"], 0,
                        f"{where}: the page scrolls sideways by "
                        f"{result['overflow']}px",
                    )
                with self.subTest(page=where, check="header rows"):
                    if width <= 640:
                        self.assertLessEqual(
                            result["headerRows"], 2,
                            f"{where}: the header takes "
                            f"{result['headerRows']} rows on a phone",
                        )
                with self.subTest(page=where, check="crowded out"):
                    self.assertEqual(
                        result["offenders"], [],
                        f"{where}: content wider than its box: "
                        f"{result['offenders']}",
                    )

    def test_english_at_every_width(self):
        self._audit("en")

    def test_chinese_at_every_width(self):
        self._audit("zh-TW")
