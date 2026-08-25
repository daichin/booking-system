"""Independent adversarial review of the preemption engine (spec §11).

Written by a reviewing agent that did not author
``app/services/preemption.py``. Every test here is meant to *break* the
engine rather than restate what ``tests/test_preemption.py`` already proves,
so overlap with that file is deliberately avoided.

Tests marked :func:`unittest.expectedFailure` document a defect that is
believed real; the comment above each one names it.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import threading
import types
import unittest
from datetime import timedelta

from app.db.base import Connection
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
from app.services import mailer
from app.services.preemption import attempt_booking
from app.timeutil import now_utc
from tests.support import AppTestCase, taipei_at


class ReviewBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.freeze_at(0, 9, 0)
        self.room = self.create_room(name="會議室 A")

    def attempt(self, user, start, end, *, confirm=False, dry_run=False,
                title="審查會議", room=None):
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

    def existing(self, user, start, end, *, title="既有會議", room=None):
        return self.create_booking(
            room=room or self.room, user=user, start_at=start, end_at=end, title=title
        )

    # --- invariants -------------------------------------------------------

    def assert_no_overlapping_confirmed(self) -> None:
        """The one invariant the whole engine exists to preserve."""
        rows = self.query_all(
            "SELECT id, room_id, start_at, end_at FROM bookings"
            " WHERE status = 'confirmed' ORDER BY room_id, start_at, end_at"
        )
        for earlier, later in zip(rows, rows[1:]):
            if earlier["room_id"] != later["room_id"]:
                continue
            self.assertLessEqual(
                earlier["end_at"],
                later["start_at"],
                f"confirmed bookings {earlier['id']} and {later['id']} overlap",
            )

    def assert_ledger_consistent(self) -> None:
        """Every displaced booking has exactly one log row and a winner."""
        preempted = self.query_all(
            "SELECT id, preempted_by_booking_id FROM bookings"
            " WHERE status = 'preempted'"
        )
        log = self.query_all("SELECT victim_booking_id FROM preemption_log")
        self.assertEqual(
            len(log), len(preempted), "preemption_log must match displaced bookings"
        )
        self.assertEqual(
            sorted(row["victim_booking_id"] for row in log),
            sorted(row["id"] for row in preempted),
        )
        for row in preempted:
            self.assertIsNotNone(row["preempted_by_booking_id"])

    def run_concurrently(self, calls):
        """Fire every callable at once; return results in submission order."""
        barrier = threading.Barrier(len(calls), timeout=20)
        results: list = [None] * len(calls)
        failures: list = []

        def run(index, call):
            try:
                barrier.wait()
                results[index] = call()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                failures.append(exc)

        threads = [
            threading.Thread(target=run, args=(index, call))
            for index, call in enumerate(calls)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "a worker thread never finished")
        self.assertEqual(failures, [], f"worker raised: {failures}")
        return results


# ---------------------------------------------------------------------------
# 1. §7.1 rules, attacked from angles the existing suite does not cover
# ---------------------------------------------------------------------------


class LevelDecisionTests(ReviewBase):
    def test_snapshot_level_must_not_make_a_senior_preemptible(self):
        """The inverse of C8, which the existing suite does not cover.

        C8 proves a *demotion* exposes a victim. The dangerous direction is a
        *promotion*: if the engine ever fell back to ``level_at_booking`` the
        victim here would look like a level 2 and be displaced.
        """
        victim_user = self.create_user(level=2)
        victim = self.existing(victim_user, taipei_at(1, 14), taipei_at(1, 15))
        self.assertEqual(victim.level_at_booking, 2)

        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET level = ? WHERE id = ?", (9, victim_user.id)
            )
        )

        challenger = self.create_user(level=5)
        result = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)

    def test_requesters_own_snapshot_level_is_irrelevant(self):
        """A requester demoted since their last booking loses their reach."""
        requester = self.create_user(level=9)
        # An old booking of theirs, elsewhere in time, snapshotting level 9.
        self.existing(requester, taipei_at(2, 9), taipei_at(2, 10))
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET level = ? WHERE id = ?", (2, requester.id)
            )
        )
        holder = self.create_user(level=5)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            self.get_user(requester.id),
            taipei_at(1, 14),
            taipei_at(1, 15),
            confirm=True,
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)

    def test_level_ten_cannot_preempt_level_ten(self):
        """The top of the ladder must still obey "strictly higher"."""
        holder = self.create_user(level=10)
        rival = self.create_user(level=10)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(rival, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)

    def test_a_one_level_gap_is_enough(self):
        holder = self.create_user(level=4)
        challenger = self.create_user(level=5)
        victim = self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )
        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)


class OverlapGeometryTests(ReviewBase):
    def test_new_booking_strictly_containing_the_victim(self):
        """Containment in the direction C4 does not test."""
        holder = self.create_user(level=2)
        challenger = self.create_user(level=6)
        victim = self.existing(holder, taipei_at(1, 14, 30), taipei_at(1, 15))

        result = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 16), confirm=True
        )
        self.assertEqual(result.outcome, CREATED)
        displaced = self.get_booking(victim.id)
        self.assertEqual(displaced.status, PREEMPTED)
        self.assertEqual(displaced.start_at, taipei_at(1, 14, 30))
        self.assertEqual(displaced.end_at, taipei_at(1, 15))
        self.assert_no_overlapping_confirmed()

    def test_a_thirty_minute_touch_on_both_sides_is_not_an_overlap(self):
        neighbour = self.create_user(level=10)
        requester = self.create_user(level=1)
        self.existing(neighbour, taipei_at(1, 13), taipei_at(1, 14))
        self.existing(neighbour, taipei_at(1, 15), taipei_at(1, 16))

        result = self.attempt(requester, taipei_at(1, 14), taipei_at(1, 15))
        self.assertEqual(result.outcome, CREATED)
        self.assert_no_overlapping_confirmed()

    def test_a_cancelled_booking_never_blocks(self):
        """Only ``confirmed`` rows occupy a room (§4.4)."""
        holder = self.create_user(level=10)
        requester = self.create_user(level=1)
        for status in ("cancelled_by_user", "cancelled_by_admin", "preempted"):
            self.create_booking(
                room=self.room,
                user=holder,
                start_at=taipei_at(1, 14),
                end_at=taipei_at(1, 15),
                status=status,
            )

        result = self.attempt(requester, taipei_at(1, 14), taipei_at(1, 15))
        self.assertEqual(result.outcome, CREATED)

    def test_an_overlap_in_another_room_is_not_an_overlap(self):
        other = self.create_room(name="會議室 B")
        holder = self.create_user(level=10)
        requester = self.create_user(level=1)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15), room=other)

        result = self.attempt(requester, taipei_at(1, 14), taipei_at(1, 15))
        self.assertEqual(result.outcome, CREATED)


class AllOrNothingTests(ReviewBase):
    def test_a_protected_blocker_in_second_position_saves_the_first_victim(self):
        """C5 blocks on level; this blocks on the protection window instead."""
        junior = self.create_user(level=2)
        other = self.create_user(level=2)
        requester = self.create_user(level=7)
        # Frozen 09:00, protection 120 min. 11:30 is outside, 10:00 is inside.
        first = self.existing(junior, taipei_at(0, 11, 30), taipei_at(0, 12))
        second = self.existing(other, taipei_at(0, 10), taipei_at(0, 10, 30))

        result = self.attempt(
            requester, taipei_at(0, 10), taipei_at(0, 12), confirm=True
        )

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, PROTECTED_WINDOW)
        self.assertEqual(self.get_booking(first.id).status, CONFIRMED)
        self.assertEqual(self.get_booking(second.id).status, CONFIRMED)
        self.assertEqual(self.query_all("SELECT id FROM preemption_log"), [])
        self.assertEqual(
            self.query_all(
                "SELECT id FROM audit_log WHERE action = 'booking_preempted'"
            ),
            [],
        )

    def test_own_booking_among_several_overlaps_refuses_the_whole_request(self):
        junior = self.create_user(level=2)
        requester = self.create_user(level=8)
        theirs = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))
        mine = self.existing(requester, taipei_at(1, 15), taipei_at(1, 16))

        result = self.attempt(
            requester, taipei_at(1, 14), taipei_at(1, 16), confirm=True
        )

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, SELF_OVERLAP)
        self.assertEqual(self.get_booking(theirs.id).status, CONFIRMED)
        self.assertEqual(self.get_booking(mine.id).status, CONFIRMED)
        self.assertEqual(self.query_all("SELECT id FROM preemption_log"), [])

    def test_three_victims_are_displaced_atomically(self):
        requester = self.create_user(level=9)
        victims = []
        for hour in (14, 15, 16):
            owner = self.create_user(level=2)
            victims.append(
                self.existing(owner, taipei_at(1, hour), taipei_at(1, hour + 1))
            )

        result = self.attempt(
            requester, taipei_at(1, 14), taipei_at(1, 17), confirm=True
        )
        self.assertEqual(result.outcome, CREATED)
        for victim in victims:
            self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)
        self.assert_ledger_consistent()
        self.assert_no_overlapping_confirmed()
        self.assertEqual(len([e for e in result.emails if e.kind == "E5"]), 3)


class ProtectionBoundaryTests(ReviewBase):
    def test_exactly_at_the_boundary_the_victim_is_already_immune(self):
        """``now >= start_at - protection`` — the boundary itself is immune."""
        self.freeze(taipei_at(0, 12))          # protection default 120 min
        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        self.existing(holder, taipei_at(0, 14), taipei_at(0, 15))

        result = self.attempt(
            challenger, taipei_at(0, 14), taipei_at(0, 15), confirm=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, PROTECTED_WINDOW)

    def test_one_second_before_the_boundary_the_victim_is_still_exposed(self):
        self.freeze(taipei_at(0, 12) - timedelta(seconds=1))
        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        victim = self.existing(holder, taipei_at(0, 14), taipei_at(0, 15))

        result = self.attempt(
            challenger, taipei_at(0, 14), taipei_at(0, 15), confirm=True
        )
        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)

    def test_protection_is_re_read_from_settings_not_cached(self):
        """An admin widening the window must take effect immediately."""
        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        self.existing(holder, taipei_at(0, 11, 30), taipei_at(0, 12))

        first = self.attempt(
            challenger, taipei_at(0, 11, 30), taipei_at(0, 12), dry_run=True
        )
        self.assertEqual(first.outcome, PREEMPTION_REQUIRED)

        self.set_setting("preemption_protection_minutes", 240)
        second = self.attempt(
            challenger, taipei_at(0, 11, 30), taipei_at(0, 12), confirm=True
        )
        self.assertEqual(second.outcome, BLOCKED)
        self.assertEqual(second.reason, PROTECTED_WINDOW)

    def test_a_long_victim_already_under_way_is_immune_even_at_zero_protection(self):
        """C7 with the requested span entirely in the future.

        The victim started at 08:30 and runs to 20:00; the request is for
        18:00-19:00, hours away. Only measuring against the *victim's* start
        makes this immune.
        """
        self.set_setting("preemption_protection_minutes", 0)
        holder = self.create_user(level=1)
        challenger = self.create_user(level=10)
        self.existing(holder, taipei_at(0, 8, 30), taipei_at(0, 20))

        result = self.attempt(
            challenger, taipei_at(0, 18), taipei_at(0, 19), confirm=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(result.reason, PROTECTED_WINDOW)


class ChainReactionTests(ReviewBase):
    def test_a_two_step_chain_keeps_both_histories(self):
        """§7.1 "chain reactions": B displaces A, then C displaces B."""
        a_user = self.create_user(level=2)
        b_user = self.create_user(level=5)
        c_user = self.create_user(level=8)

        a = self.existing(a_user, taipei_at(1, 14), taipei_at(1, 15))
        first = self.attempt(b_user, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertEqual(first.outcome, CREATED)
        second = self.attempt(c_user, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertEqual(second.outcome, CREATED)

        # A's row still points at B's booking, not C's: history is not rewritten.
        self.assertEqual(
            self.get_booking(a.id).preempted_by_booking_id, first.booking.id
        )
        self.assertEqual(
            self.get_booking(first.booking.id).preempted_by_booking_id,
            second.booking.id,
        )
        self.assertEqual(self.get_booking(first.booking.id).status, PREEMPTED)
        self.assert_ledger_consistent()
        self.assert_no_overlapping_confirmed()


# ---------------------------------------------------------------------------
# 2. Two-phase behaviour (§7.2)
# ---------------------------------------------------------------------------


class TwoPhaseReviewTests(ReviewBase):
    TABLES = ("bookings", "preemption_log", "audit_log", "email_log")

    def test_phase_one_writes_nothing_anywhere(self):
        """Existing tests check bookings; audit_log and email_log matter too."""
        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        before = {
            table: self.query_all(f"SELECT id FROM {table}") for table in self.TABLES
        }
        self.attempt(challenger, taipei_at(1, 14), taipei_at(1, 15), dry_run=True)
        self.attempt(challenger, taipei_at(1, 16), taipei_at(1, 17), dry_run=True)
        after = {
            table: self.query_all(f"SELECT id FROM {table}") for table in self.TABLES
        }
        self.assertEqual(before, after)

    def test_phase_one_confirmation_flag_is_ignored(self):
        """``dry_run`` must win over ``confirm_preemption`` — never write."""
        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        victim = self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        result = attempt_booking(
            self.db,
            requester_id=challenger.id,
            room_id=self.room.id,
            start_at=taipei_at(1, 14),
            end_at=taipei_at(1, 15),
            title="審查會議",
            confirm_preemption=True,
            dry_run=True,
        )
        self.assertEqual(result.outcome, PREEMPTION_REQUIRED)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)
        self.assertEqual(
            self.query_all("SELECT id FROM bookings WHERE status='confirmed'"),
            [{"id": victim.id}],
        )

    def test_phase_two_does_not_invent_victims_that_vanished(self):
        """Phase 1 said PREEMPTION_REQUIRED; the victim then self-cancels.

        Phase 2 must commit a plain booking with no displacement and, above
        all, no E5 to a member whose booking was never taken from them.
        """
        from app.services.bookings import cancel_booking

        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        victim = self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        phase_one = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
        )
        self.assertEqual(phase_one.outcome, PREEMPTION_REQUIRED)

        cancel_booking(self.db, actor=holder, booking_id=victim.id)

        phase_two = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )
        self.assertEqual(phase_two.outcome, CREATED)
        self.assertEqual(phase_two.victims, [])
        self.assertEqual([e.kind for e in phase_two.emails], ["E4"])
        self.assertEqual(self.query_all("SELECT id FROM preemption_log"), [])

    def test_phase_two_revalidates_the_requesters_own_standing(self):
        """Suspended between the dialog and the confirmation click.

        §6.5 step 1 has to be re-run inside the commit transaction too, not
        only during the check.
        """
        from app.errors import AppError, NOT_ACTIVE

        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        victim = self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        self.assertEqual(
            self.attempt(
                challenger, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
            ).outcome,
            PREEMPTION_REQUIRED,
        )
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE users SET status = 'suspended' WHERE id = ?", (challenger.id,)
            )
        )

        with self.assertRaises(AppError) as ctx:
            self.attempt(challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertErrorCode(ctx, NOT_ACTIVE)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)
        self.assertEqual(self.query_all("SELECT id FROM preemption_log"), [])

    def test_phase_two_revalidates_the_room(self):
        """A room deactivated between the two phases must abort the commit."""
        from app.errors import AppError, ROOM_INACTIVE

        holder = self.create_user(level=2)
        challenger = self.create_user(level=8)
        victim = self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        self.assertEqual(
            self.attempt(
                challenger, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
            ).outcome,
            PREEMPTION_REQUIRED,
        )
        self.db.run_in_transaction(
            lambda conn: conn.execute(
                "UPDATE rooms SET is_active = ? WHERE id = ?", (False, self.room.id)
            )
        )

        with self.assertRaises(AppError) as ctx:
            self.attempt(challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertErrorCode(ctx, ROOM_INACTIVE)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)

    def test_phase_two_discovers_a_victim_phase_one_never_saw(self):
        """Phase 1 said AVAILABLE; someone books the slot first.

        Without ``confirm_preemption`` the commit must stop and ask, never
        silently displace a booking the user was never shown.
        """
        challenger = self.create_user(level=8)
        phase_one = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), dry_run=True
        )
        self.assertEqual(phase_one.outcome, AVAILABLE)

        holder = self.create_user(level=2)
        victim = self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        phase_two = self.attempt(challenger, taipei_at(1, 14), taipei_at(1, 15))
        self.assertEqual(phase_two.outcome, PREEMPTION_REQUIRED)
        self.assertEqual(self.get_booking(victim.id).status, CONFIRMED)
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status='confirmed'")), 1
        )


# ---------------------------------------------------------------------------
# 3. Privacy (§7.2, §12 C11)
# ---------------------------------------------------------------------------


class PrivacyReviewTests(ReviewBase):
    ADDRESS = "do.not.leak@example.com"

    def test_no_serialised_response_carries_an_email_address(self):
        holder = self.create_user(level=2, email=self.ADDRESS, full_name="持有者")
        challenger = self.create_user(level=8)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        for kwargs in ({"dry_run": True}, {"confirm": False}):
            with self.subTest(kwargs=kwargs):
                result = self.attempt(
                    challenger, taipei_at(1, 14), taipei_at(1, 15), **kwargs
                )
                self.assertNotIn(self.ADDRESS, json.dumps(result.to_dict()))

        committed = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )
        self.assertEqual(committed.outcome, CREATED)
        self.assertNotIn(self.ADDRESS, json.dumps(committed.to_dict()))

    def test_a_blocked_response_carries_no_email_address(self):
        holder = self.create_user(level=9, email=self.ADDRESS)
        challenger = self.create_user(level=2)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(
            challenger, taipei_at(1, 14), taipei_at(1, 15), confirm=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertNotIn(self.ADDRESS, json.dumps(result.to_dict()))

    def test_self_overlap_blocker_exposes_nothing_about_anyone(self):
        member = self.create_user(level=5, email=self.ADDRESS)
        self.existing(member, taipei_at(1, 14), taipei_at(1, 16))

        result = self.attempt(member, taipei_at(1, 15), taipei_at(1, 16), confirm=True)
        self.assertEqual(result.reason, SELF_OVERLAP)
        self.assertNotIn("owner", result.blocker)
        self.assertNotIn(self.ADDRESS, json.dumps(result.to_dict()))

    def test_the_e5_notice_never_names_the_winner_even_with_several_victims(self):
        winner = self.create_user(
            level=9, full_name="王大明", email="winner@example.com"
        )
        for hour in (14, 15):
            owner = self.create_user(level=2, full_name=f"受害者{hour}")
            self.existing(owner, taipei_at(1, hour), taipei_at(1, hour + 1))

        result = self.attempt(winner, taipei_at(1, 14), taipei_at(1, 16), confirm=True)
        notices = [e for e in result.emails if e.kind == "E5"]
        self.assertEqual(len(notices), 2)
        for notice in notices:
            body = json.dumps(notice.context, default=str)
            self.assertNotIn("王大明", body)
            self.assertNotIn("winner@example.com", body)
            self.assertNotIn(winner.id, body)

    def test_the_rendered_e5_subject_and_body_never_name_the_winner(self):
        """C11 is about what actually lands in the inbox, not just context."""
        from app.services import email_templates

        winner = self.create_user(
            level=9, full_name="王大明", email="winner@example.com"
        )
        owner = self.create_user(level=2, full_name="受害者")
        self.existing(owner, taipei_at(1, 14), taipei_at(1, 15))

        result = self.attempt(winner, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        notice = next(e for e in result.emails if e.kind == "E5")
        rendered = email_templates.render(notice.kind, notice.context)
        text = f"{rendered.subject}\n{rendered.text}\n{rendered.html}"
        self.assertNotIn("王大明", text)
        self.assertNotIn("winner@example.com", text)


# ---------------------------------------------------------------------------
# 4. Reminder withdrawal (§12 C10)
# ---------------------------------------------------------------------------


class ReminderWithdrawalTests(ReviewBase):
    def queue_reminder(self, booking, email):
        def work(conn):
            conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " dedupe_key, related_booking_id, attempts, created_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?)",
                (new_id(), email, "E10", "提醒",
                 f"reminder:{booking.id}", booking.id, now_utc()),
            )

        self.db.run_in_transaction(work)

    def reminder_status(self, booking):
        row = self.query_one(
            "SELECT status FROM email_log WHERE dedupe_key = ?",
            (f"reminder:{booking.id}",),
        )
        return row["status"] if row else None

    def test_a_refused_request_leaves_every_reminder_queued(self):
        junior = self.create_user(level=2)
        senior = self.create_user(level=9)
        requester = self.create_user(level=5)
        first = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))
        second = self.existing(senior, taipei_at(1, 15), taipei_at(1, 16))
        self.queue_reminder(first, junior.email)
        self.queue_reminder(second, senior.email)

        result = self.attempt(
            requester, taipei_at(1, 14), taipei_at(1, 16), confirm=True
        )
        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(self.reminder_status(first), "queued")
        self.assertEqual(self.reminder_status(second), "queued")

    def test_withdrawal_touches_only_the_displaced_bookings_reminder(self):
        junior = self.create_user(level=2)
        bystander = self.create_user(level=2)
        requester = self.create_user(level=9)
        victim = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))
        untouched = self.existing(bystander, taipei_at(1, 16), taipei_at(1, 17))
        self.queue_reminder(victim, junior.email)
        self.queue_reminder(untouched, bystander.email)

        self.attempt(requester, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertEqual(self.reminder_status(victim), "skipped")
        self.assertEqual(self.reminder_status(untouched), "queued")

    def test_a_failed_reminder_row_is_not_resurrected_or_rewritten(self):
        junior = self.create_user(level=2)
        requester = self.create_user(level=9)
        victim = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))

        def work(conn):
            conn.execute(
                "INSERT INTO email_log (id, to_email, type, subject, status,"
                " dedupe_key, related_booking_id, attempts, created_at, error)"
                " VALUES (?, ?, ?, ?, 'failed', ?, ?, 3, ?, 'provider down')",
                (new_id(), junior.email, "E10", "提醒",
                 f"reminder:{victim.id}", victim.id, now_utc()),
            )

        self.db.run_in_transaction(work)
        self.attempt(requester, taipei_at(1, 14), taipei_at(1, 15), confirm=True)
        self.assertEqual(self.reminder_status(victim), "failed")

    def test_the_reminder_job_will_not_mail_a_displaced_booking(self):
        """End-to-end C10: withdrawal plus the job's own re-check."""
        from app.services.transports import FakeTransport

        junior = self.create_user(level=2)
        requester = self.create_user(level=9)
        # Frozen 09:00, lead 60 min: a 09:30 start is inside the reminder
        # window, and preemptible only once protection is set to zero.
        self.set_setting("preemption_protection_minutes", 0)
        victim = self.existing(junior, taipei_at(0, 9, 30), taipei_at(0, 10, 30))

        self.attempt(
            requester, taipei_at(0, 9, 30), taipei_at(0, 10, 30), confirm=True
        )
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)

        transport = FakeTransport()
        mailer.run_reminders(self.db, transport=transport)
        to_victim = [
            message for message in transport.sent
            if message.to_email == junior.email and message.subject.startswith("提醒")
        ]
        self.assertEqual(to_victim, [])


# ---------------------------------------------------------------------------
# 5. Transaction safety: retries and email ordering
# ---------------------------------------------------------------------------


class RetryAndCommitOrderTests(ReviewBase):
    def test_a_retried_transaction_applies_its_effects_exactly_once(self):
        """``run_in_transaction`` retries ``work``; nothing may double-apply.

        The first attempt is failed with a retryable "database is locked"
        error *after* the winning booking row has already been inserted, which
        is the worst possible moment for a non-idempotent callable.
        """
        junior = self.create_user(level=2)
        requester = self.create_user(level=9)
        victim = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))

        original = Connection.execute
        state = {"fired": False}

        def flaky(conn_self, sql, params=None):
            if not state["fired"] and "INSERT INTO preemption_log" in sql:
                state["fired"] = True
                raise sqlite3.OperationalError("database is locked")
            return original(conn_self, sql, params)

        Connection.execute = flaky
        try:
            result = self.attempt(
                requester, taipei_at(1, 14), taipei_at(1, 15), confirm=True
            )
        finally:
            Connection.execute = original

        self.assertTrue(state["fired"], "the injected failure never triggered")
        self.assertEqual(result.outcome, CREATED)
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status='confirmed'")), 1
        )
        self.assertEqual(self.get_booking(victim.id).status, PREEMPTED)
        self.assertEqual(len(self.query_all("SELECT id FROM preemption_log")), 1)
        self.assertEqual(
            len(self.query_all(
                "SELECT id FROM audit_log WHERE action = 'booking_preempted'"
            )),
            1,
        )
        self.assertEqual(
            len(self.query_all("SELECT id FROM email_log WHERE type = 'E5'")), 1
        )
        self.assertEqual(
            len(self.query_all("SELECT id FROM email_log WHERE type = 'E4'")), 1
        )
        # The abandoned attempt must leave no orphan booking row behind.
        self.assertEqual(len(self.query_all("SELECT id FROM bookings")), 2)
        self.assert_ledger_consistent()

    def test_no_email_is_enqueued_before_the_commit(self):
        """Read the world from a *different* connection at enqueue time.

        If the enqueue happened inside the writing transaction, that reader
        would not yet see the displacement (and, under ``BEGIN IMMEDIATE``,
        would block on the write lock until it timed out).
        """
        junior = self.create_user(level=2)
        requester = self.create_user(level=9)
        victim = self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))

        seen: dict = {}
        original = mailer.enqueue

        def spy(db, events):
            box: dict = {}

            def read():
                box["rows"] = self.db.run_in_transaction(
                    lambda conn: conn.query_all("SELECT id, status FROM bookings")
                )

            thread = threading.Thread(target=read)
            thread.start()
            thread.join(timeout=15)
            seen["alive"] = thread.is_alive()
            seen["rows"] = box.get("rows")
            return original(db, events)

        mailer.enqueue = spy
        try:
            result = self.attempt(
                requester, taipei_at(1, 14), taipei_at(1, 15), confirm=True
            )
        finally:
            mailer.enqueue = original

        self.assertEqual(result.outcome, CREATED)
        self.assertFalse(seen["alive"], "the independent reader was blocked by a lock")
        statuses = {row["id"]: row["status"] for row in seen["rows"]}
        self.assertEqual(statuses[victim.id], PREEMPTED)
        self.assertEqual(statuses[result.booking.id], CONFIRMED)

    def test_a_refused_request_enqueues_nothing_at_all(self):
        junior = self.create_user(level=2)
        senior = self.create_user(level=9)
        requester = self.create_user(level=5)
        self.existing(junior, taipei_at(1, 14), taipei_at(1, 15))
        self.existing(senior, taipei_at(1, 15), taipei_at(1, 16))

        calls: list = []
        original = mailer.enqueue

        def spy(db, events):
            calls.append(events)
            return original(db, events)

        mailer.enqueue = spy
        try:
            result = self.attempt(
                requester, taipei_at(1, 14), taipei_at(1, 16), confirm=True
            )
        finally:
            mailer.enqueue = original

        self.assertEqual(result.outcome, BLOCKED)
        self.assertEqual(calls, [])
        self.assertEqual(self.query_all("SELECT id FROM email_log"), [])


# ---------------------------------------------------------------------------
# 6. Concurrency on SQLite
# ---------------------------------------------------------------------------


class ConcurrencyReviewTests(ReviewBase):
    def test_four_overlapping_but_distinct_spans_never_double_book(self):
        """Not the identical-span case the existing suite covers.

        Four requesters at four different levels ask for four different,
        mutually overlapping spans at once. Whatever order they land in, the
        room may never hold two overlapping confirmed bookings.
        """
        spans = [
            (taipei_at(1, 14), taipei_at(1, 15)),
            (taipei_at(1, 14, 30), taipei_at(1, 15, 30)),
            (taipei_at(1, 15), taipei_at(1, 16)),
            (taipei_at(1, 13, 30), taipei_at(1, 17)),
        ]
        users = [self.create_user(level=level) for level in (3, 5, 7, 9)]
        calls = [
            (lambda user=user, span=span: self.attempt(
                user, span[0], span[1], confirm=True
            ))
            for user, span in zip(users, spans)
        ]

        results = self.run_concurrently(calls)
        self.assertTrue(any(r.outcome == CREATED for r in results))
        self.assert_no_overlapping_confirmed()
        self.assert_ledger_consistent()

    def test_a_concurrent_chain_leaves_one_holder_and_a_clean_ledger(self):
        """A holds the slot; B (higher) and C (higher still) attack at once.

        Either C wins outright, or B wins and C then chains onto B. Both are
        legal; what is not legal is two confirmed bookings, or a displaced
        booking with no log row.
        """
        holder = self.create_user(level=2)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))
        challengers = [self.create_user(level=5), self.create_user(level=8)]

        calls = [
            (lambda user=user: self.attempt(
                user, taipei_at(1, 14), taipei_at(1, 15), confirm=True
            ))
            for user in challengers
        ]
        results = self.run_concurrently(calls)

        confirmed = self.query_all(
            "SELECT id, user_id FROM bookings WHERE status = 'confirmed'"
        )
        self.assertEqual(len(confirmed), 1)
        self.assert_ledger_consistent()
        self.assert_no_overlapping_confirmed()
        # The level-8 challenger can never be the one left out.
        self.assertEqual(
            results[1].outcome, CREATED,
            "the highest level must end up holding the slot",
        )
        self.assertEqual(confirmed[0]["user_id"], challengers[1].id)

    def test_a_mixed_crowd_of_preempting_and_blocked_requests(self):
        """Seven threads: some can preempt, some cannot, one is self-overlap."""
        holder = self.create_user(level=4)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 16))

        equals = [self.create_user(level=4) for _ in range(2)]
        lowers = [self.create_user(level=1) for _ in range(2)]
        highers = [self.create_user(level=6) for _ in range(2)]

        calls = []
        for user in equals + lowers + highers:
            calls.append(
                lambda user=user: self.attempt(
                    user, taipei_at(1, 15), taipei_at(1, 16), confirm=True
                )
            )
        calls.append(
            lambda: self.attempt(
                holder, taipei_at(1, 15), taipei_at(1, 16), confirm=True
            )
        )

        results = self.run_concurrently(calls)
        created = [r for r in results if r.outcome == CREATED]
        self.assertEqual(len(created), 1, [r.outcome for r in results])
        self.assertIn(created[0], (results[4], results[5]))
        # The equal-level and lower-level requests must all be refused.
        for index in (0, 1, 2, 3):
            self.assertEqual(results[index].outcome, BLOCKED)
            self.assertEqual(results[index].reason, EQUAL_OR_HIGHER_LEVEL)
        # The holder's own attempt is a self-overlap either way.
        self.assertEqual(results[6].outcome, BLOCKED)
        self.assert_no_overlapping_confirmed()
        self.assert_ledger_consistent()

    def test_concurrent_attempts_produce_exactly_one_e5_per_displacement(self):
        """One victim must never receive two "your booking was cancelled" notices."""
        holder = self.create_user(level=2)
        self.existing(holder, taipei_at(1, 14), taipei_at(1, 15))
        challengers = [self.create_user(level=5) for _ in range(4)]

        calls = [
            (lambda user=user: self.attempt(
                user, taipei_at(1, 14), taipei_at(1, 15), confirm=True
            ))
            for user in challengers
        ]
        self.run_concurrently(calls)

        notices = self.query_all(
            "SELECT id FROM email_log WHERE type = 'E5' AND to_email = ?",
            (holder.email,),
        )
        self.assertEqual(len(notices), 1)
        self.assertEqual(len(self.query_all("SELECT id FROM preemption_log")), 1)
        self.assert_no_overlapping_confirmed()

    def test_many_threads_and_random_spans_never_break_the_invariant(self):
        """Randomised stress: the shapes a hand-written case would not think of.

        The inputs are seeded so a failure is reproducible; the thread
        interleaving is not, which is the point.
        """
        rng = random.Random(20260826)

        for _round in range(3):
            calls = []
            for _ in range(8):
                user = self.create_user(level=rng.randint(1, 10))
                start_slot = rng.randrange(0, 8)          # 13:00 .. 16:30
                length = rng.randrange(1, 5)              # 30 .. 120 minutes
                start = taipei_at(1, 13) + timedelta(minutes=30 * start_slot)
                end = start + timedelta(minutes=30 * length)
                calls.append(
                    lambda user=user, start=start, end=end: self.attempt(
                        user, start, end, confirm=True
                    )
                )
            self.run_concurrently(calls)
            self.assert_no_overlapping_confirmed()
            self.assert_ledger_consistent()

        # Something must actually have happened, or the test proves nothing.
        self.assertTrue(
            self.query_all("SELECT id FROM bookings WHERE status = 'confirmed'")
        )
        self.assertTrue(self.query_all("SELECT id FROM preemption_log"))

    def test_a_free_slot_contested_by_five_threads_yields_one_booking(self):
        """More than two contenders, all on an empty slot."""
        users = [self.create_user(level=5) for _ in range(5)]
        calls = [
            (lambda user=user: self.attempt(user, taipei_at(1, 14), taipei_at(1, 15)))
            for user in users
        ]
        results = self.run_concurrently(calls)

        created = [r for r in results if r.outcome == CREATED]
        self.assertEqual(len(created), 1)
        self.assertEqual(
            len(self.query_all("SELECT id FROM bookings WHERE status='confirmed'")), 1
        )
        # The four losers meet an equal-level holder, never a silent overwrite.
        for result in results:
            if result.outcome != CREATED:
                self.assertEqual(result.outcome, BLOCKED)
                self.assertEqual(result.reason, EQUAL_OR_HIGHER_LEVEL)


# ---------------------------------------------------------------------------
# 7. The Postgres path, which cannot be executed on this machine
# ---------------------------------------------------------------------------


class _FakePgCursor:
    def __init__(self, connection) -> None:
        self._connection = connection
        self.description = None
        self._rows: list = []

    def execute(self, sql, params=None):
        self._connection.run(sql)
        self.description = [("value",)]
        self._rows = [(1,)]
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakePgConnection:
    """Models the two behaviours that decide the isolation level.

    1. **psycopg 3** with ``autocommit=False`` opens a transaction implicitly
       before the first command it is asked to run; it does not inspect the
       SQL, so a literal ``BEGIN ...`` is just another command.
    2. **PostgreSQL** answers ``BEGIN`` inside an open transaction with
       ``WARNING 25001: there is already a transaction in progress`` and
       ignores it. The isolation level of the running transaction is
       unchanged.
    """

    def __init__(self, autocommit: bool) -> None:
        self.autocommit = autocommit
        self.in_transaction = False
        self.isolation_level: str | None = None
        self.statements: list[str] = []
        self.ignored_begins: list[str] = []
        self.closed = False

    def run(self, sql: str) -> None:
        text = " ".join(str(sql).split())
        upper = text.upper()
        if upper in ("COMMIT", "ROLLBACK"):
            self.statements.append(text)
            self.in_transaction = False
            self.isolation_level = None
            return
        if not self.autocommit and not self.in_transaction:
            # psycopg's own implicit BEGIN, at the server default level.
            self.statements.append("BEGIN")
            self.in_transaction = True
            self.isolation_level = "read committed"
        self.statements.append(text)
        if upper.startswith("BEGIN"):
            if self.in_transaction:
                # PostgreSQL: WARNING 25001, the statement is ignored and the
                # running transaction keeps its existing isolation level.
                self.ignored_begins.append(text)
            else:
                # A BEGIN that really does open the transaction, which is only
                # possible when psycopg has not already opened one for us.
                self.in_transaction = True
                self.isolation_level = (
                    "serializable" if "SERIALIZABLE" in upper else "read committed"
                )

    def cursor(self):
        return _FakePgCursor(self)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self.run("COMMIT")

    def rollback(self):
        self.run("ROLLBACK")

    def close(self):
        self.closed = True


def _fake_psycopg(created: list):
    module = types.ModuleType("psycopg")

    class SerializationFailure(Exception):
        pass

    class DeadlockDetected(Exception):
        pass

    errors = types.SimpleNamespace(
        SerializationFailure=SerializationFailure,
        DeadlockDetected=DeadlockDetected,
    )

    def connect(dsn, autocommit=False):
        connection = _FakePgConnection(autocommit)
        created.append(connection)
        return connection

    module.connect = connect          # type: ignore[attr-defined]
    module.errors = errors            # type: ignore[attr-defined]
    return module


class PostgresIsolationTests(unittest.TestCase):
    """The production backend's write-serialisation guarantee.

    Neon/Postgres cannot be reached from this machine, so the driver is
    replaced with a model of psycopg 3 + PostgreSQL faithful enough to decide
    the one question that matters: which isolation level the preemption
    transaction actually runs at.
    """

    def build(self):
        from app.db.base import PostgresDatabase

        created: list = []
        saved = sys.modules.get("psycopg")
        sys.modules["psycopg"] = _fake_psycopg(created)
        self.addCleanup(
            lambda: sys.modules.__setitem__("psycopg", saved)
            if saved is not None
            else sys.modules.pop("psycopg", None)
        )
        return PostgresDatabase("postgresql://example/db"), created

    # DEFECT (found by this review, since FIXED in app/db/base.py):
    # `PostgresDatabase` used to connect with `autocommit=False` and then
    # issue `BEGIN ISOLATION LEVEL SERIALIZABLE` as an ordinary statement.
    # psycopg 3 had already opened a transaction by then, so PostgreSQL
    # answered with `WARNING 25001: there is already a transaction in
    # progress` and ignored the BEGIN -- leaving the preemption transaction
    # running at READ COMMITTED. `FOR UPDATE` locks only rows that already
    # exist, so two concurrent requests for the same *free* slot would both
    # see an empty overlap set and both commit, silently double-booking a
    # room in production while every SQLite test stayed green.
    #
    # The fix connects with autocommit=True so the explicit BEGIN genuinely
    # opens the transaction and sets its isolation level. This test guards
    # against the regression.
    def test_the_transaction_actually_runs_at_serializable(self):
        db, created = self.build()

        observed: list = []
        db.run_in_transaction(lambda conn: observed.append(conn.raw.isolation_level))

        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0].ignored_begins,
            [],
            "PostgreSQL ignored the explicit BEGIN: "
            f"statements were {created[0].statements}",
        )
        self.assertEqual(observed, ["serializable"])
