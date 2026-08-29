"""The tutorial must describe the system that actually exists.

Nothing here touches the database or the services: the tutorial is scripted
content, and the risk is not that it breaks but that it quietly stops being
true. That is exactly what happened when room closures were added -- a new
admin tab appeared in the real console and the tutorial went on showing the
old list, even though `tutorial_content.py` says in a comment that its list
matches the real nav.

Every check here is a scanner over "the tutorial and the app agree", because
a hand-written assertion per screen would have to be remembered too, and
being remembered is the thing that failed.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from app.i18n.en import STRINGS
from app.web import tutorial_content
from app.web.layout import STYLESHEET
from app.web.pages.admin_pages import _ADMIN_NAV

ROOT = pathlib.Path(__file__).resolve().parent.parent
OFFLINE = ROOT / "tutorial" / "offline.html"


class NavigationParityTests(unittest.TestCase):
    def test_the_admin_tabs_match_the_real_console(self):
        """The tutorial walks someone through clicking these tabs. A list that
        is missing one teaches a console that does not exist."""
        real = [STRINGS[key] for _, key in _ADMIN_NAV]
        self.assertEqual(
            tutorial_content._ADMIN_SUBNAV,
            real,
            "the tutorial's admin tabs have drifted from app/web/pages/admin_pages.py",
        )

    def test_every_header_variant_matches_the_real_navigation(self):
        """All three, because they differ in more than length: the tutorial
        link comes last for everyone, which for an admin puts it *after*
        "Admin" -- so the admin list cannot be the member list plus one."""
        from app.web.layout import _nav_items

        def nav_for(user):
            request = type("R", (), {"user": user, "path": "/day"})()
            return [caption for _, caption in _nav_items(request)]

        member = type("U", (), {"is_admin": False})()
        admin = type("U", (), {"is_admin": True})()

        for name, declared, real in (
            ("logged out", tutorial_content._HEADER_LOGGED_OUT, nav_for(None)),
            ("member", tutorial_content._HEADER_MEMBER, nav_for(member)),
            ("admin", tutorial_content._HEADER_ADMIN, nav_for(admin)),
        ):
            with self.subTest(header=name):
                self.assertEqual(
                    declared, real,
                    f"the tutorial's {name} nav has drifted from app/web/layout.py",
                )


class ContentIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = tutorial_content.TUTORIAL_STEPS

    def test_every_step_has_a_unique_id(self):
        ids = [step["id"] for step in self.steps]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(duplicates, [], f"repeated step ids: {duplicates}")

    def test_every_step_belongs_to_a_known_track(self):
        for step in self.steps:
            with self.subTest(step=step["id"]):
                self.assertIn(step["track"], tutorial_content.TRACKS)

    def test_every_slot_kind_is_one_the_renderer_can_draw(self):
        """A kind the script does not handle renders as an unstyled row, which
        looks like a bug in the real product rather than a typo here."""
        handled = set(
            re.findall(r"slot\.kind === '([a-z]+)'", tutorial_content.TUTORIAL_SCRIPT)
        )
        used = {
            slot["kind"]
            for step in self.steps
            for slot in step["screen_data"].get("slots", [])
        }
        self.assertEqual(
            used - handled, set(), f"the renderer cannot draw: {used - handled}"
        )

    def test_every_class_the_grid_paints_exists_in_the_stylesheet(self):
        """The offline file inlines the real stylesheet, so a class the
        tutorial invents would silently render flat."""
        painted = set(
            re.findall(r"classes\.push\('([a-z-]+)'\)", tutorial_content.TUTORIAL_SCRIPT)
        )
        for name in painted:
            with self.subTest(css_class=name):
                self.assertIn(
                    f".{name}", STYLESHEET, f".{name} is not styled anywhere"
                )


class OfflineFileTests(unittest.TestCase):
    """`tutorial/offline.html` is generated and committed, so it can go stale
    the moment somebody edits the content and forgets to rebuild it."""

    def test_it_is_in_step_with_the_content(self):
        self.assertTrue(OFFLINE.is_file(), "tutorial/offline.html is missing")
        html = OFFLINE.read_text(encoding="utf-8")

        match = re.search(
            r'<script type="application/json" id="tutorial-steps">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match, "the offline file has no step data")
        embedded = json.loads(match.group(1).replace("\\u003c", "<"))

        self.assertEqual(
            [step["id"] for step in embedded],
            [step["id"] for step in tutorial_content.TUTORIAL_STEPS],
            "tutorial/offline.html is stale -- run `python manage.py tutorial-build`",
        )

    def test_it_carries_the_stylesheet_the_app_uses(self):
        html = OFFLINE.read_text(encoding="utf-8")
        self.assertIn(
            ".slot.is-closed",
            html,
            "the offline file predates the closed-slot style -- rebuild it",
        )
