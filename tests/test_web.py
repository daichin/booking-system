"""Web layer: routing, CSRF, security headers, health, and the reminder cron."""

from __future__ import annotations

from app.config import Config
from app.web.app import bootstrap, create_app
from tests.support import AppTestCase
from tests.webclient import Client


class WebTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = Config(
            base_url="http://testserver",
            cron_secret="cron-secret-value",
            admin_email="owner@example.com",
            admin_initial_password="an initial password",
            email_transport="fake",
        )
        self.app = create_app(self.db, self.config)
        self.client = Client(self.app)


class RoutingTests(WebTestBase):
    def test_root_redirects_anonymous_visitors_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/login")

    def test_unknown_path_is_a_404(self):
        self.assertEqual(self.client.get("/no-such-page").status, 404)

    def test_wrong_method_is_a_405_not_a_404(self):
        self.assertEqual(self.client.post("/api/health").status, 405)

    def test_security_headers_are_always_present(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.header("X-Frame-Options"), "DENY")
        self.assertIn("default-src 'self'", response.header("Content-Security-Policy"))

    def test_a_csrf_cookie_is_issued_on_first_contact(self):
        self.client.get("/")
        self.assertTrue(self.client.cookies.get("csrf"))


class HealthTests(WebTestBase):
    def test_health_reports_version_and_database(self):
        payload = self.client.get("/api/health").json()
        self.assertEqual(payload["database"], "ok")
        self.assertIn("version", payload)
        self.assertIn("time", payload)

    def test_health_reports_the_last_reminder_run(self):
        # Nothing has run yet on a fresh database.
        self.assertIsNone(self.client.get("/api/health").json()["last_reminder_run"])

        self.client.post(
            "/api/cron/send-reminders",
            headers={"HTTP_X_CRON_SECRET": "cron-secret-value"},
        )
        self.assertIsNotNone(self.client.get("/api/health").json()["last_reminder_run"])


class CronEndpointTests(WebTestBase):
    def test_the_reminder_endpoint_requires_the_shared_secret(self):
        self.assertEqual(self.client.post("/api/cron/send-reminders").status, 403)

    def test_a_wrong_secret_is_refused(self):
        response = self.client.post(
            "/api/cron/send-reminders", headers={"HTTP_X_CRON_SECRET": "guess"}
        )
        self.assertEqual(response.status, 403)

    def test_the_correct_secret_runs_the_job(self):
        response = self.client.post(
            "/api/cron/send-reminders",
            headers={"HTTP_X_CRON_SECRET": "cron-secret-value"},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("reminders", response.json())


class BootstrapTests(WebTestBase):
    def test_bootstrap_creates_the_admin_and_example_rooms(self):
        result = bootstrap(self.db, self.config)
        self.assertEqual(result["admin"], "created")
        self.assertEqual(result["rooms_seeded"], 3)

        admin = self.query_one(
            "SELECT * FROM users WHERE email = ?", ("owner@example.com",)
        )
        self.assertTrue(admin["is_admin"])
        # Spec §10.3: the seeded password must be changed at first login.
        self.assertTrue(admin["must_change_password"])

    def test_bootstrap_is_idempotent(self):
        bootstrap(self.db, self.config)
        again = bootstrap(self.db, self.config)
        self.assertEqual(again["admin"], "exists")
        self.assertEqual(again["rooms_seeded"], 0)
        self.assertEqual(
            int(self.query_one("SELECT COUNT(*) AS n FROM rooms")["n"]), 3
        )
        self.assertEqual(
            int(self.query_one("SELECT COUNT(*) AS n FROM users")["n"]), 1
        )

    def test_bootstrap_without_secrets_skips_the_admin(self):
        result = bootstrap(self.db, Config())
        self.assertEqual(result["admin"], "skipped_no_secrets")
