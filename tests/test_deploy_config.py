"""Deployment configuration (spec §10).

These run offline. They cannot prove a deploy works -- that needs real
provider accounts (see CONTRACT.md §8) -- but they do stop the pipeline
rotting: a renamed secret, a lost schedule, or a workflow that starts
deploying on every push would all be caught here.

The workflows are checked as text because PyYAML is not in the standard
library and the app takes no third-party dependencies.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from app.config import REQUIRED_SECRETS

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

#: The host's deploy token, which spec §10.2 requires in addition to the
#: eight named secrets.
DEPLOY_HOOK_SECRET = "RENDER_DEPLOY_HOOK_URL"

_CJK = re.compile(r"[一-鿿]")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class WorkflowFilesTests(unittest.TestCase):
    def test_all_three_workflows_exist(self):
        for name in ("deploy.yml", "reminders.yml", "ci.yml"):
            with self.subTest(workflow=name):
                self.assertTrue((WORKFLOWS / name).is_file(), f"missing {name}")


class DeployWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(WORKFLOWS / "deploy.yml")

    def test_it_is_a_manual_button(self):
        # Spec C3: the owner presses one button in the GitHub web UI.
        self.assertIn("workflow_dispatch:", self.text)

    def test_it_does_not_deploy_on_push(self):
        # A push trigger would deploy unreviewed work and violate C3's intent.
        trigger_block = self.text.split("jobs:", 1)[0]
        self.assertNotRegex(trigger_block, r"^\s*push:", )

    def test_every_required_secret_is_passed_in(self):
        for secret in REQUIRED_SECRETS:
            with self.subTest(secret=secret):
                self.assertIn(f"secrets.{secret}", self.text)

    def test_the_deploy_hook_secret_is_used_and_checked(self):
        self.assertIn(f"secrets.{DEPLOY_HOOK_SECRET}", self.text)
        self.assertIn(DEPLOY_HOOK_SECRET, self.text)

    def test_it_runs_the_required_steps_in_order(self):
        # Spec §10.1: install, lint, test, migrate, deploy, smoke-test.
        expected = [
            "check-secrets",
            "pip install -r requirements.txt",
            "compileall",
            "unittest discover",
            "manage.py migrate",
            # The deploy step itself. The hook secret is also *validated*
            # much earlier so the run fails fast, so match the curl call
            # rather than the bare secret name.
            'curl -fsS -X POST "$RENDER_DEPLOY_HOOK_URL"',
            "manage.py health",
        ]
        positions = []
        for needle in expected:
            index = self.text.find(needle)
            self.assertNotEqual(index, -1, f"deploy.yml never mentions {needle!r}")
            positions.append(index)
        self.assertEqual(
            positions, sorted(positions), "deploy steps are out of order"
        )

    def test_the_smoke_test_tolerates_a_cold_start(self):
        # Render free services take up to ~60s to wake (spec §2.1).
        self.assertIn("--retries", self.text)
        self.assertIn("/api/health", self.text)

    def test_it_prints_the_live_url_in_the_job_summary(self):
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)
        self.assertIn("APP_BASE_URL", self.text)


class ReminderWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(WORKFLOWS / "reminders.yml")

    def test_it_runs_every_fifteen_minutes(self):
        self.assertIn("*/15 * * * *", self.text)

    def test_it_calls_the_protected_endpoint_with_the_shared_secret(self):
        self.assertIn("/api/cron/send-reminders", self.text)
        self.assertIn("X-Cron-Secret", self.text)
        self.assertIn("secrets.CRON_SECRET", self.text)

    def test_it_tolerates_a_sixty_second_cold_start(self):
        self.assertIn("--max-time", self.text)
        self.assertIn("--retry", self.text)


class CiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(WORKFLOWS / "ci.yml")

    def test_it_exercises_a_real_postgres(self):
        # The build machine has no Postgres, so CI is the only place the
        # production backend actually runs.
        self.assertIn("postgres:16", self.text)
        self.assertIn("services:", self.text)

    def test_the_postgres_job_actually_points_the_suite_at_postgres(self):
        # Without this the job would silently re-run the SQLite suite.
        self.assertIn("TEST_DATABASE_URL", self.text)

    def test_the_harness_honours_that_variable(self):
        support = read(ROOT / "tests" / "support.py")
        self.assertIn("TEST_DATABASE_URL", support)


class DocumentationTests(unittest.TestCase):
    def test_setup_and_rollback_exist_and_are_substantial(self):
        for name in ("SETUP.md", "ROLLBACK.md"):
            with self.subTest(document=name):
                path = ROOT / name
                self.assertTrue(path.is_file(), f"missing {name}")
                self.assertGreater(len(read(path)), 1000, f"{name} is too thin")

    def test_they_are_written_in_chinese(self):
        # Spec §10.1: written in Traditional Chinese for a non-developer.
        for name in ("SETUP.md", "ROLLBACK.md"):
            with self.subTest(document=name):
                text = read(ROOT / name)
                self.assertGreater(
                    len(_CJK.findall(text)), 200, f"{name} is not written in Chinese"
                )

    def test_setup_documents_every_secret_by_name(self):
        text = read(ROOT / "SETUP.md")
        for secret in (*REQUIRED_SECRETS, DEPLOY_HOOK_SECRET):
            with self.subTest(secret=secret):
                self.assertIn(secret, text)

    def test_setup_explains_the_forced_password_change(self):
        # Spec §10.3 requires SETUP.md to say so.
        text = read(ROOT / "SETUP.md")
        self.assertIn("ADMIN_INITIAL_PASSWORD", text)
        self.assertIn("密碼", text)

    def test_setup_documents_the_github_schedule_limitation(self):
        # Spec §9.3 names this as a known limitation that must be documented.
        text = read(ROOT / "SETUP.md")
        self.assertIn("60 天", text)

    def test_rollback_explains_redeploying_from_the_web_ui(self):
        text = read(ROOT / "ROLLBACK.md")
        self.assertIn("Run workflow", text)
        self.assertIn("Actions", text)


class ManageCliTests(unittest.TestCase):
    """`check-secrets` is what makes spec §12 E2 pass, so prove it."""

    def _run(self, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for secret in (*REQUIRED_SECRETS, DEPLOY_HOOK_SECRET):
            env.pop(secret, None)
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "manage.py", "check-secrets"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_it_fails_and_names_every_missing_secret(self):
        result = self._run({})
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        for secret in REQUIRED_SECRETS:
            with self.subTest(secret=secret):
                self.assertIn(secret, combined)

    def test_it_names_the_one_secret_that_is_missing(self):
        present = {secret: "value" for secret in REQUIRED_SECRETS}
        del present["CRON_SECRET"]
        result = self._run(present)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("CRON_SECRET", combined)
        self.assertNotIn("DATABASE_URL", combined)

    def test_it_succeeds_when_everything_is_present(self):
        result = self._run({secret: "value" for secret in REQUIRED_SECRETS})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PackagingTests(unittest.TestCase):
    def test_production_requirements_are_pinned(self):
        text = read(ROOT / "requirements.txt")
        self.assertIn("psycopg", text)
        self.assertIn("gunicorn", text)
        for line in text.splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                self.assertIn("==", entry, f"unpinned dependency: {entry}")

    def test_the_start_command_matches_the_wsgi_factory(self):
        for name in ("Procfile", "render.yaml"):
            with self.subTest(file=name):
                self.assertIn("app.web.app:build_wsgi_app()", read(ROOT / name))

    def test_render_does_not_auto_deploy_on_push(self):
        self.assertIn("autoDeploy: false", read(ROOT / "render.yaml"))
