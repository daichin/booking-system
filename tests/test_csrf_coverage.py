"""Every rendered POST form must carry a usable CSRF token.

A form that forgets the hidden field is not a broken form -- it is a button
that always fails with a 403 and no explanation. That is what shipped for
"create room": the helper that built it never received the request, so it
could not include the token, and no test rendered it.

Rather than one test per form, these crawl the real pages and check every
form they find, so a form added later is covered the day it is written.
"""

from __future__ import annotations

import re

from app.config import Config
from app.web.app import create_app
from app.web.framework import CSRF_COOKIE, CSRF_FIELD
from tests.support import AppTestCase
from tests.webclient import Client

_PASSWORD = "a decent passphrase"

#: Pages that render forms. Anonymous pages first, then member, then admin.
ANONYMOUS_PAGES = ["/login", "/register", "/forgot"]
MEMBER_PAGES = ["/day", "/week", "/my", "/account", "/password"]
ADMIN_PAGES = [
    "/admin",
    "/admin/approvals",
    "/admin/members",
    "/admin/invitations",
    "/admin/rooms",
    "/admin/bookings",
    "/admin/preemptions",
    "/admin/settings",
    "/admin/emails",
    "/admin/audit",
]

_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_HIDDEN = re.compile(r'<input[^>]*name="' + CSRF_FIELD + r'"[^>]*>')
_VALUE = re.compile(r'value="([^"]*)"')


def post_forms(html: str) -> list[tuple[str, str]]:
    """``(attributes, inner_html)`` for every form that submits a POST."""
    return [
        (attrs, body)
        for attrs, body in _FORM.findall(html)
        if 'method="post"' in attrs.lower()
    ]


class CsrfCoverageTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)
        self.create_room(name="會議室 A")

    def _login(self, *, admin: bool) -> None:
        email = "admin@example.com" if admin else "member@example.com"
        self.create_user(email=email, password=_PASSWORD, is_admin=admin)
        self.client.get("/login")
        self.client.post("/login", form={"email": email, "password": _PASSWORD})

    def _assert_forms_are_submittable(self, paths: list[str]) -> None:
        expected = self.client.cookies.get(CSRF_COOKIE)
        checked = 0
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status, 200, f"{path} did not render")
            for attrs, body in post_forms(response.text):
                action = re.search(r'action="([^"]*)"', attrs)
                where = f"{path} -> form action={action.group(1) if action else '(self)'}"
                with self.subTest(form=where):
                    tag = _HIDDEN.search(body)
                    self.assertIsNotNone(tag, f"{where} has no {CSRF_FIELD} field")
                    value = _VALUE.search(tag.group(0))
                    self.assertIsNotNone(value, f"{where} has no value attribute")
                    self.assertTrue(value.group(1), f"{where} has an empty token")
                    if expected:
                        self.assertEqual(
                            value.group(1), expected, f"{where} token is stale"
                        )
                    checked += 1
        self.assertGreater(checked, 0, "no POST forms were found to check")

    def test_anonymous_pages(self):
        self._assert_forms_are_submittable(ANONYMOUS_PAGES)

    def test_member_pages(self):
        self._login(admin=False)
        self._assert_forms_are_submittable(MEMBER_PAGES)

    def test_admin_pages(self):
        self._login(admin=True)
        self._assert_forms_are_submittable(ADMIN_PAGES)

    def test_the_create_room_form_can_actually_submit_itself(self):
        """The specific regression: this form shipped with no token at all."""
        self._login(admin=True)
        page = self.client.get("/admin/rooms")
        token = _VALUE.search(_HIDDEN.search(page.text).group(0)).group(1)
        self.assertTrue(token)

        response = self.client.post(
            "/admin/rooms",
            form={
                "name": "新會議室",
                "capacity": "8",
                "location": "4 樓",
                "equipment_note": "白板",
                "open_time": "",
                "close_time": "",
            },
        )
        self.assertNotEqual(
            response.status, 403, "the create-room form was refused as cross-site"
        )
        created = self.query_all(
            "SELECT name FROM rooms WHERE name = ?", ("新會議室",)
        )
        self.assertEqual(len(created), 1)


class FirstVisitTests(AppTestCase):
    """A visitor's very first page must already be submittable.

    The token used to come from the request cookie, which does not exist yet
    on a first visit -- so the page rendered an empty token while the same
    response set a real cookie, and the first submit was always refused.
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )

    def test_a_form_on_the_very_first_response_carries_the_cookie_token(self):
        client = Client(self.app)          # no cookies at all
        page = client.get("/login")

        tag = _HIDDEN.search(page.text)
        self.assertIsNotNone(tag, "the login form has no CSRF field")
        rendered = _VALUE.search(tag.group(0)).group(1)

        self.assertTrue(rendered, "first-visit form rendered an empty token")
        self.assertEqual(
            rendered,
            client.cookies.get(CSRF_COOKIE),
            "the rendered token and the cookie just set disagree",
        )

    def test_submitting_straight_from_a_first_visit_is_not_refused(self):
        self.create_user(email="first@example.com", password=_PASSWORD)
        client = Client(self.app)
        client.get("/login")
        response = client.post(
            "/login", form={"email": "first@example.com", "password": _PASSWORD}
        )
        self.assertNotEqual(response.status, 403)


_FIELD = re.compile(r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"')


class DuplicateFieldTests(AppTestCase):
    """No form may submit two fields of the same name.

    The browser sends both, and the parser keeps the first. That is how
    changing a member's level came to fail every time: the row carried a
    hidden "level" filter, the select was also called "level", and the empty
    filter won. Nothing errored -- the wrong value was simply used.

    Checked across every page rather than for that one form, because the
    mistake is invisible in the markup and easy to repeat.
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(
            self.db, Config(base_url="http://testserver", email_transport="fake")
        )
        self.client = Client(self.app)
        self.room = self.create_room(name="會議室 A")
        self.member = self.create_user(email="member@example.com", password=_PASSWORD)

    def _login(self, *, admin: bool) -> None:
        email = "admin@example.com" if admin else "member@example.com"
        if admin:
            self.create_user(email=email, password=_PASSWORD, is_admin=True)
        self.client.get("/login")
        self.client.post("/login", form={"email": email, "password": _PASSWORD})

    def _assert_no_duplicates(self, paths: list[str]) -> None:
        checked = 0
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status, 200, f"{path} did not render")
            for attrs, body in post_forms(response.text):
                action = re.search(r'action="([^"]*)"', attrs)
                where = f"{path} -> {action.group(1) if action else '(self)'}"
                names = _FIELD.findall(body)
                repeated = sorted({n for n in names if names.count(n) > 1})
                with self.subTest(form=where):
                    self.assertEqual(
                        repeated, [], f"{where} submits {repeated} more than once"
                    )
                checked += 1
        self.assertGreater(checked, 0, "no forms were checked")

    def test_member_pages(self):
        self._login(admin=False)
        self._assert_no_duplicates(MEMBER_PAGES)

    def test_admin_pages(self):
        self._login(admin=True)
        self._assert_no_duplicates(ADMIN_PAGES)

    def test_anonymous_pages(self):
        self._assert_no_duplicates(ANONYMOUS_PAGES)
