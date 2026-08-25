"""The preemption engine (spec §7, acceptance group C).

Group C is the critical set: it is the part of the system the spec calls
highest-risk, so each scenario is named after the acceptance test it proves.
"""

from __future__ import annotations

import json
import threading

from app.errors import (
    AVAILABLE,
    BLOCKED,
    CREATED,
    EQUAL_OR_HIGHER_LEVEL,
    PREEMPTION_REQUIRED,
    PROTECTED_WINDOW,
    SELF_OVERLAP,
)
from app.models import CONFIRMED, PREEMPTED, new_id
from app.services import preemption
from app.services.preemption import attempt_booking
from app.timeutil import now_utc
from tests.support import AppTestCase, taipei_at


class PreemptionTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.freeze_at(0, 9, 0)
        self.room = self.create_room(name="會議室 A")

    def attempt(self, user, start, end, *, confirm=False, dry_run=False,
                title="重要會議", room=None):
        return attempt_booking(
            self.db,
            requester_id=user.id,
            room_id=(room or self.room).id,
            start_at=start,
            end_at=end,
            title=title,
            confirm_preemption=confirm,
            dry_run=dry_run,
        )

    def existing(self, user, start, end, title="既有會議"):
        """A confirmed booking placed directly, bypassing the engine."""
        return self.create_booking(
            room=self.room, user=user, start_at=start, end_at=end, title=title
        )

    def preemption_rows(self):
        return self.query_all("SELECT * FROM preemption_log")


class LevelRuleTests(PreemptionTestBase):
    def test_c1_higher_level_preempts_lower(self):
        victim_user = self.create_user(level=3, full_name="低階同事")
        winner_user = self.create_user(level=5, full_name="高階同事")
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            winner_user, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )

        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(result.booking.status, CONFIRMED)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)
        self.assertEqual(
            self.get_booking(victim.id).preempted_by_booking_id, result.booking.id
        )

        rows = self.preemption_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["victim_level"]), 3)
        self.assertEqual(int(rows[0]["winner_level"]), 5)

        kinds = [event.kind for event in result.emails]
        self.assertIn("E4", kinds)   # confirmation to the winner
        self.assertIn("E5", kinds)   # displacement notice to the victim

    def test_c2_equal_level_can_never_preempt(self):
        victim_user = self.create_user(level=3)
        rival = self.create_user(level=3)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(rival, taipei_at(1, 14), taipei_at(1, 15), confirm=True)

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)
        self.assertEqual(self.preemption_rows(), [])

    def test_c3_lower_level_cannot_preempt_higher(self):
        victim_user = self.create_user(level=7)
        challenger = self.create_user(level=5)
        self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)

    def test_c8_current_level_decides_not_the_level_at_booking(self):
        # Booked while senior, then demoted: the demotion must expose them.
        victim_user = self.create_user(level=8)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))
        self.assertEqual(victim.level_at_booking, 8)

        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET level = ? WHERE id = ?", (2, victim_user.id)
            )
        )

        winner = self.create_user(level=5)
        result = self.attempt(winner, taipei_at(1, 14), taipei_at(1, 15), confirm=True)

        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)
        # The log records the level at the moment of preemption, not at booking.
        self.assertEqual(int(self.preemption_rows()[0]["victim_level"]), 2)

    def test_admin_status_grants_no_preemption_privilege(self):
        victim_user = self.create_user(level=5)
        admin = self.create_user(level=3, is_admin=True)
        self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(admin, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)

    def test_c12_a_member_cannot_preempt_their_own_booking(self):
        member = self.create_user(level=5)
        own = self.existing(member, taipei_at(1, 14), taipei_at(1, 16))

        result = self.attempt(member, taipei_at(1, 15), taipei_at(1, 16), confirm=True)

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, SELF_OVERLAP)
        self.assertEqual(self.get_booking(own.id).status, CONFIRMED)


class OverlapShapeTests(PreemptionTestBase):
    def test_c4_partial_overlap_cancels_the_whole_victim_booking(self):
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 16))

        result = self.attempt(
            winner, taipei_at(1, 15, 30), taipei_at(1, 16, 30), confirm=True
        )

        self.assertEqual(result.outcome, CREATED)
        displaced = self.get_booking(victim.id)
        self.assertEqual(displaced.status, PREEMPTED)
        # No splitting or trimming: the original span is untouched on the row.
        self.assertEqual(displaced.start_at, taipei_at(1, 14))
        self.assertEqual(displaced.end_at, taipei_at(1, 16))

    def test_c5_all_or_nothing_across_several_victims(self):
        junior = self.create_user(level=2)
        senior = self.create_user(level=9)
        requester = self.create_user(level=5)

        junior_booking = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))
        senior_booking = self.existing(senior, taipei_at(1, 15), taipei_at(1, 16))

        result = self.attempt(
            requester, taipei_at(1, 14), taipei_at(1, 16), confirm=True
        )

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)
        # The preemptible one must be left completely untouched.
        self.assertEqual(self.get_booking(junior_booking.id).status, CONFIRMED)
        self.assertEqual(self.get_booking(senior_booking.id).status, CONFIRMED)
        self.assertEqual(self.preemption_rows(), [])
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status = 'confirmed'")), 2
        )

    def test_several_preemptible_victims_are_all_displaced(self):
        first = self.create_user(level=2)
        second = self.create_user(level=3)
        requester = self.create_user(level=6)
        a = self.existing(first, taipei_at(1, 14), taipei_at(1, 15))
        b = self.existing(second, taipei_at(1, 15), taipei_at(1, 16))

        result = self.attempt(
            requester, taipei_at(1, 14), taipei_at(1, 16), confirm=True
        )

        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(self.get_booking(a.id).status, PREEMPTED)
        self.assertEqual(self.get_booking(b.id).status, PREEMPTED)
        self.assertEqual(len(self.preemption_rows()), 2)
        self.assertEqual(len([e for e in result.emails if e.kind == "E5"]), 2)

    def test_touching_bookings_do_not_overlap(self):
        neighbour = self.create_user(level=9)
        requester = self.create_user(level=1)
        self.existing(neighbour, taipei_at(1, 14), taipei_at(1, 15))

        # 15:00-16:00 starts exactly when the other ends.
        result = self.attempt(requester, taipei_at(1, 15), taipei_at(1, 16))
        self.assertEqual(result.outcome, CREATED)


class ProtectionWindowTests(PreemptionTestBase):
    def test_c6_booking_inside_the_protection_window_is_immune(self):
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        # Frozen at 09:00; this starts in 90 minutes, inside the 120 default.
        self.existing(victim_user, taipei_at(0, 10, 30), taipei_at(0, 11, 30))

        result = self.attempt(
            winner, taipei_at(0, 10, 30), taipei_at(0, 11, 30), confirm=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, PROTECTED_WINDOW)

    def test_c6_booking_outside_the_protection_window_can_be_preempted(self):
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        # Starts in 150 minutes, beyond the 120-minute window.
        victim = self.existing(victim_user, taipei_at(0, 11, 30), taipei_at(0, 12, 30))

        result = self.attempt(
            winner, taipei_at(0, 11, 30), taipei_at(0, 12, 30), confirm=True
        )
        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)

    def test_c6_zero_protection_allows_preemption_right_up_to_the_start(self):
        self.set_setting("preemption_protection_minutes", 0)
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(0, 10, 30), taipei_at(0, 11, 30))

        result = self.attempt(
            winner, taipei_at(0, 10, 30), taipei_at(0, 11, 30), confirm=True
        )
        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)

    def test_protection_window_is_measured_against_the_victims_start(self):
        # The victim starts inside the window; the *new* booking starts well
        # outside it. Measuring against the wrong one would wrongly allow this.
        self.set_setting("preemption_protection_minutes", 120)
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        self.existing(victim_user, taipei_at(0, 10), taipei_at(0, 18))

        result = self.attempt(winner, taipei_at(0, 16), taipei_at(0, 17), confirm=True)
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, PROTECTED_WINDOW)

    def test_c7_a_booking_already_under_way_can_never_be_preempted(self):
        self.set_setting("preemption_protection_minutes", 0)
        victim_user = self.create_user(level=1)
        winner = self.create_user(level=10)
        # Frozen at 09:00: this began at 08:30 and runs to 12:00.
        self.existing(victim_user, taipei_at(0, 8, 30), taipei_at(0, 12))

        result = self.attempt(winner, taipei_at(0, 10), taipei_at(0, 11), confirm=True)
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, PROTECTED_WINDOW)


class TwoPhaseTests(PreemptionTestBase):
    def test_phase_one_reports_available_without_writing(self):
        user = self.create_user(level=5)
        result = self.attempt(user, taipei_at(1, 14), taipei_at(1, 15), dry_run=True)
        self.assertEqual(result.outcome, AVAILABLE)
        self.assertEqual(self.query_all("SELECT id FROM bookings"), [])

    def test_phase_one_lists_victims_without_writing(self):
        victim_user = self.create_user(level=3, full_name="王小明", department="業務部")
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            winner, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
        )

        self.assertEqual(result.outcome, PREEMPTION_REQUIRED)
        self.assertEqual(len(result.victims), 1)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)
        self.assertEqual(self.query_all("SELECT id FROM preemption_log"), [])

        # The dialog may show name and department, never an email address.
        payload = result.victims[0].for_client()
        self.assertEqual(payload["owner"]["full_name"], "王小明")
        self.assertEqual(payload["owner"]["department"], "業務部")
        self.assertNotIn("email", json.dumps(payload))

    def test_an_unconfirmed_commit_changes_nothing(self):
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(winner, taipei_at(1, 14), taipei_at(1, 15), confirm=False)

        self.assertEqual(result.outcome, PREEMPTION_REQUIRED)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status = 'confirmed'")), 1
        )

    def test_phase_one_blocked_reveals_no_email_address(self):
        victim_user = self.create_user(
            level=9, full_name="陳大文", department="財務部",
            email="secret.person@example.com",
        )
        challenger = self.create_user(level=2)
        self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertNotIn("secret.person@example.com", json.dumps(result.blocker))
        self.assertEqual(result.blocker["owner"]["full_name"], "陳大文")

    def test_phase_two_is_re_evaluated_and_not_trusted(self):
        # Phase 1 says preemption is possible; the victim is then promoted
        # above the requester before phase 2 runs.
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        phase_one = self.attempt(
            winner, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
        )
        self.assertEqual(phase_one.outcome, PREEMPTION_REQUIRED)

        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET level = ? WHERE id = ?", (9, victim_user.id)
            )
        )

        phase_two = self.attempt(
            winner, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )
        self.assertEqual(phase_two.outcome, BLOCKED)
        self.assertEqual(phase_two.reason, EQUAL_OR_HIGHER_LEVEL)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)


class NotificationTests(PreemptionTestBase):
    def test_c11_the_victim_is_not_told_who_displaced_them(self):
        victim_user = self.create_user(
            level=3, full_name="受害同事", email="victim@example.com"
        )
        winner = self.create_user(
            level=5, full_name="王大明", email="winner@example.com"
        )
        self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            winner, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )

        notice = next(e for e in result.emails if e.kind == "E5")
        self.assertEqual(notice.to_email, "victim@example.com")
        serialised = json.dumps(notice.context, default=str)
        self.assertNotIn("王大明", serialised)
        self.assertNotIn("winner@example.com", serialised)
        # It must still say which room and when.
        self.assertEqual(notice.context["room_name"], "會議室 A")
        self.assertEqual(notice.context["reason"], "preempted")

    def test_c10_preemption_withdraws_the_victims_pending_reminder(self):
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        def queue_reminder(conn):
            conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " dedupe_key, related_booking_id, attempts, created_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?)",
                (
                    new_id(), victim_user.email, "E10", "提醒",
                    f"reminder:{victim.id}", victim.id, now_utc(),
                ),
            )

        self.db.run_in_transaction(queue_reminder)

        self.attempt(winner, taipei_at(1, 14), taipei_at(1, 15), confirm=True)

        row = self.query_one(
            "SELECT status FROM email_log WHERE dedupe_key = ?",
            (f"reminder:{victim.id}",),
        )
        self.assertEqual(row["status"], "skipped")

    def test_an_already_sent_reminder_is_left_alone(self):
        victim_user = self.create_user(level=3)
        winner = self.create_user(level=5)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))

        def already_sent(conn):
            conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " dedupe_key, related_booking_id, attempts, created_at, sent_at)"
                " VALUES (?, ?, ?, ?, 'sent', ?, ?, 1, ?, ?)",
                (
                    new_id(), victim_user.email, "E10", "提醒",
                    f"reminder:{victim.id}", victim.id, now_utc(), now_utc(),
                ),
            )

        self.db.run_in_transaction(already_sent)
        self.attempt(winner, taipei_at(1, 14), taipei_at(1, 15), confirm=True)

        row = self.query_one(
            "SELECT status FROM email_log WHERE dedupe_key = ?",
            (f"reminder:{victim.id}",),
        )
        self.assertEqual(row["status"], "sent")


class ConcurrencyTests(PreemptionTestBase):
    def test_c9_two_simultaneous_preemptions_produce_exactly_one_winner(self):
        """Spec §12 C9.

        Both challengers sit at the same level, one above the victim. Whoever
        commits first takes the slot; the other then faces an equal-level
        holder and must be refused rather than chaining a second preemption.
        That is what makes "one winner, one victim record, one E5" observable.
        """
        victim_user = self.create_user(level=3)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))
        challengers = [self.create_user(level=5) for _ in range(2)]

        barrier = threading.Barrier(len(challengers))
        results: list = []
        lock = threading.Lock()

        def run(user) -> None:
            barrier.wait(timeout=10)
            outcome = self.attempt(
                user, taipei_at(1, 14), taipei_at(1, 15), confirm=True
            )
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=run, args=(user,)) for user in challengers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(results), 2)
        created = [r for r in results if r.outcome == CREATED]
        blocked = [r for r in results if r.outcome == BLOCKED]

        self.assertEqual(len(created), 1, "exactly one booking may be created")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].reason, EQUAL_OR_HIGHER_LEVEL)

        # One victim record, one displacement, one notice.
        self.assertEqual(len(self.preemption_rows()), 1)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status = 'confirmed'")),
            1,
        )
        notices = [e for e in created[0].emails if e.kind == "E5"]
        self.assertEqual(len(notices), 1)

    def test_two_simultaneous_bookings_for_a_free_slot_yield_one_booking(self):
        first, second = self.create_user(level=4), self.create_user(level=4)
        barrier = threading.Barrier(2)
        results: list = []
        lock = threading.Lock()

        def run(user) -> None:
            barrier.wait(timeout=10)
            outcome = self.attempt(user, taipei_at(1, 14), taipei_at(1, 15))
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=run, args=(u,)) for u in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        created = [r for r in results if r.outcome == CREATED]
        self.assertEqual(len(created), 1)
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status = 'confirmed'")),
            1,
        )
