"""Acceptance traceability (spec §12).

Spec §12 is the definition of done, and Task 8's job is to make sure every
scenario is actually covered rather than assumed. Rather than re-implementing
the scenarios a third time, this module walks the whole suite and asserts that
each numbered scenario has at least one test named after it -- so deleting or
renaming the test that proves C5 breaks the build instead of quietly reducing
coverage.

Group E is the exception: it needs a real deploy against real provider
accounts, which cannot run here (CONTRACT.md §8). What *can* be checked
offline is that every artifact Group E depends on exists and is wired up, and
that the manual steps are documented for the owner.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Spec §12, verbatim. The text is kept here so a failure message can say
#: which scenario is unproven rather than just printing an id.
SCENARIOS: dict[str, str] = {
    # Group A -- accounts
    "A1": "Self-registration sends E1; the link activates verification exactly once",
    "A2": "A link older than 24h is rejected and resend issues a fresh one",
    "A3": "A verified user cannot book; the UI explains they await approval",
    "A4": "After admin approval the user receives E2 and can book",
    "A5": "An invited user registers and can book immediately",
    "A6": "A revoked or expired invitation link is rejected",
    "A7": "Password reset invalidates existing sessions",
    "A8": "Six failed logins in ten minutes triggers rate limiting",
    # Group B -- booking basics
    "B1": "14:00-15:30 succeeds; 14:10-15:00 is rejected as off-grid",
    "B2": "A booking exceeding max_booking_minutes is rejected",
    "B3": "A booking beyond booking_horizon_days is rejected",
    "B4": "A booking outside the room's open/close window is rejected",
    "B5": "A level-1 user with quota 3 is blocked on the fourth booking",
    "B6": "Cancelling frees the slot immediately for another user",
    # Group C -- preemption (critical)
    "C1": "Level 5 preempts level 3; E5 sent; log records both levels",
    "C2": "Level 3 cannot preempt level 3 -- EQUAL_OR_HIGHER_LEVEL",
    "C3": "Level 5 cannot preempt level 7",
    "C4": "Partial overlap cancels the entire victim booking",
    "C5": "All-or-nothing: one non-preemptible victim rejects the whole request",
    "C6": "Protection window: 90 minutes immune, 150 minutes preemptible, 0 disables",
    "C7": "A booking that has already started can never be preempted",
    "C8": "Current level decides, not level_at_booking",
    "C9": "Two simultaneous attempts yield exactly one winner and one E5",
    "C10": "Preemption cancels the victim's pending E10 reminder",
    "C11": "E5 names the room and time but not the preempting user",
    "C12": "A user cannot preempt their own booking",
    # Group D -- email
    "D1": "Reminders fire once and only once per booking",
    "D2": "A cancelled booking's reminder is not sent",
    "D3": "Exceeding daily_email_cap drops reminders but still sends E1/E5/E8/E9",
    "D4": "Two registrations within an hour produce one batched digest",
    "D5": "A provider failure is retried and surfaced in the admin email log",
}

#: Group E needs a real deploy; it is verified by the owner, not by CI.
MANUAL_SCENARIOS: dict[str, str] = {
    "E1": "Run workflow on a clean fork with all secrets produces a live site",
    "E2": "A missing secret fails the workflow with a message naming it",
    "E3": "First deploy creates the admin, forced to change password",
    "E4": "Re-running the deploy is safe and does not duplicate seed data",
    "E5": "A non-technical reader can follow SETUP.md end to end",
}


def collect_test_names() -> set[str]:
    """Every test method name in the suite, lower-cased."""
    loader = unittest.defaultTestLoader
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    names: set[str] = set()

    def walk(item) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            names.add(item.id().rsplit(".", 1)[-1].lower())

    walk(suite)
    return names


class TraceabilityTests(unittest.TestCase):
    """Each §12 scenario must be proven by a test named after it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.names = collect_test_names()
        # Fail loudly rather than silently passing on an empty discovery.
        assert len(cls.names) > 50, f"test discovery found only {len(cls.names)} tests"

    def _tests_for(self, scenario: str) -> list[str]:
        prefix = f"test_{scenario.lower()}_"
        return [name for name in self.names if name.startswith(prefix)]

    def test_every_scenario_has_a_named_test(self):
        missing = {
            scenario: description
            for scenario, description in SCENARIOS.items()
            if not self._tests_for(scenario)
        }
        if missing:
            report = "\n".join(
                f"  {scenario}: {description}" for scenario, description in
                sorted(missing.items())
            )
            self.fail(
                f"{len(missing)} acceptance scenario(s) have no test named "
                f"test_<id>_...:\n{report}"
            )

    def test_the_critical_group_is_fully_covered(self):
        # Spec §12: "Group C (preemption) is the critical set."
        for scenario in [key for key in SCENARIOS if key.startswith("C")]:
            with self.subTest(scenario=scenario):
                self.assertTrue(
                    self._tests_for(scenario),
                    f"{scenario} ({SCENARIOS[scenario]}) is unproven",
                )

    def test_scenario_ids_are_not_accidentally_shared(self):
        # "C1" must not be satisfied by a test that is really about C10.
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                for name in self._tests_for(scenario):
                    suffix = name[len(f"test_{scenario.lower()}_"):]
                    self.assertFalse(
                        suffix[:1].isdigit(),
                        f"{name} looks like it belongs to a different scenario",
                    )


class DeploymentScenarioTests(unittest.TestCase):
    """Group E cannot run here; check everything it depends on instead."""

    def test_group_e_artifacts_all_exist(self):
        for relative in (
            ".github/workflows/deploy.yml",
            "SETUP.md",
            "ROLLBACK.md",
            "manage.py",
            "requirements.txt",
        ):
            with self.subTest(artifact=relative):
                self.assertTrue((ROOT / relative).is_file(), f"missing {relative}")

    def test_group_e_is_documented_as_a_manual_step(self):
        # The owner must know these five remain their job.
        contract = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("Group E", contract)

    def test_e3_forced_password_change_is_covered_by_an_automated_test(self):
        # E3's *behaviour* is testable offline even though the deploy is not.
        names = collect_test_names()
        self.assertTrue(
            any("must_change_password" in name or "forced" in name for name in names),
            "no test covers the forced first-login password change (spec §10.3)",
        )

    def test_e4_repeat_deploy_safety_is_covered_by_an_automated_test(self):
        names = collect_test_names()
        self.assertTrue(
            any("idempotent" in name for name in names),
            "no test covers re-running the deploy safely (spec §12 E4)",
        )

    @unittest.skip(
        "Spec §12 Group E requires a real deploy with real provider accounts. "
        "The build machine has no network (CONTRACT.md §8), so the owner must "
        "run it once from the GitHub Actions tab -- see SETUP.md."
    )
    def test_group_e_end_to_end(self):  # pragma: no cover - documentation
        raise AssertionError("unreachable")
