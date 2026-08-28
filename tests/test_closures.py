"""Closing part of a day rather than a whole room.

The rule is stored once and evaluated per day, so most of the risk is in the
evaluation rather than in the storage: a weekday origin off by one, a date
taken in UTC instead of Taipei, or an overlap test that disagrees with itself
depending on which end you ask from. Those three are scanners rather than
one-off cases, because each of them is a whole family of bugs.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.config import Config
from app.errors import (
    AppError,
    CONFIRMATION_REQUIRED,
    ForbiddenError,
    MISSING_FIELD,
    NotFoundError,
    ROOM_CLOSED,
)
from app.models import CANCELLED_BY_ADMIN, CONFIRMED
from app.services import bookings as bookings_service
from app.services import closures, preemption, rooms
from app.settings import Settings
from app.timeutil import combine_taipei, local_date, now_utc
from tests.support import AppTestCase, taipei_at
from tests.webclient import Client

_PASSWORD = "a decent passphrase"


class ClosureTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.room = self.create_room(name="Meeting Room 1")
        self.admin = self.create_user(
            email="admin@example.com", password=_PASSWORD, is_admin=True, level=1
        )
        self.member = self.create_user(
            email="member@example.com", password=_PASSWORD, level=10
        )
        self.today = local_date(now_utc())

    def close(self, *, days_ahead=3, span_days=0, start="12:00", end="15:00",
              weekdays=closures.ALL_WEEKDAYS, reason="", cancel=False):
        first = self.today + timedelta(days=days_ahead)
        last = first + timedelta(days=span_days)
        return closures.create_closure(
            self.db, self.admin,
            room_ids=[self.room.id],
            from_date=first.isoformat(),
            to_date=last.isoformat(),
            start_time=start, end_time=end,
            weekdays=weekdays, reason=reason, cancel_bookings=cancel,
        )

    def closed_on(self, day: date) -> list[tuple[int, int]]:
        got = self.db.run_in_transaction(
            lambda conn: closures.for_day(conn, self.room.id, day)
        )
        return [(c.start_minutes, c.end_minutes) for c in got]

    def attempt(self, user, hour_from, hour_to, days_ahead=3):
        return preemption.attempt_booking(
            self.db,
            requester_id=user.id,
            room_id=self.room.id,
            start_at=taipei_at(days_ahead, hour_from),
            end_at=taipei_at(days_ahead, hour_to),
            title="Planning",
            dry_run=True,
        )


class OverlapScannerTests(ClosureTestBase):
    """The half-open rule, asserted from both directions at once.

    The same question is asked by booking validation ("may this go here?") and
    by closure creation ("what is in the way?"). Writing it twice is how the
    two drift apart, and the drift shows up as a booking that blocks a closure
    it would not itself have been blocked by. One table, both directions.
    """

    #: (from_hour, to_hour, overlaps a 12:00-15:00 closure)
    CASES = [
        (10, 12, False),   # ends exactly where the closure starts
        (11, 13, True),    # straddles the start
        (12, 15, True),    # exactly the closure
        (13, 14, True),    # wholly inside
        (14, 16, True),    # straddles the end
        (15, 16, False),   # starts exactly where the closure ends
        (9, 11, False),    # entirely before
        (16, 17, False),   # entirely after
    ]

    def test_validation_and_conflict_detection_agree(self):
        for from_hour, to_hour, should_overlap in self.CASES:
            with self.subTest(booking=f"{from_hour:02d}:00-{to_hour:02d}:00"):
                self.setUp()  # a clean room per case
                booking = self.create_booking(
                    room=self.room,
                    user=self.member,
                    start_at=taipei_at(3, from_hour),
                    end_at=taipei_at(3, to_hour),
                )

                # Direction 1: does the closure see this booking as in the way?
                day = self.today + timedelta(days=3)
                found = self.db.run_in_transaction(
                    lambda conn: closures.conflicting_bookings(
                        conn,
                        room_id=self.room.id,
                        from_date=day,
                        to_date=day,
                        start_minutes=12 * 60,
                        end_minutes=15 * 60,
                        weekday_mask=closures.ALL_WEEKDAYS,
                    )
                )
                self.assertEqual(
                    [row["id"] for row in found],
                    [booking.id] if should_overlap else [],
                    "conflict detection disagrees with the table",
                )

                # Direction 2: would validation refuse the same booking?
                self.db.run_in_transaction(
                    lambda conn: conn.execute(
                        "UPDATE bookings SET status = ? WHERE id = ?",
                        (CANCELLED_BY_ADMIN, booking.id),
                    )
                )
                self.close()
                refused = False
                try:
                    self.attempt(self.member, from_hour, to_hour)
                except AppError as exc:
                    refused = exc.code == ROOM_CLOSED
                self.assertEqual(
                    refused, should_overlap, "validation disagrees with the table"
                )


class WeekdayScannerTests(ClosureTestBase):
    """Monday = 0, the origin ``date.weekday()`` uses.

    The most likely single bug in this feature: Monday is 0 in Python, Sunday
    is 0 in SQLite's strftime, and Monday is 1 in ISO. One table kills the
    whole family.
    """

    def test_each_single_weekday_closes_only_that_weekday(self):
        # A fortnight starting on a known Monday, so every weekday appears twice.
        monday = self.today + timedelta(days=(7 - self.today.weekday()) % 7 + 7)
        for weekday in range(7):
            with self.subTest(weekday=weekday):
                self.setUp()
                closures.create_closure(
                    self.db, self.admin,
                    room_ids=[self.room.id],
                    from_date=monday.isoformat(),
                    to_date=(monday + timedelta(days=13)).isoformat(),
                    start_time="08:00", end_time="10:00",
                    weekdays={weekday},
                )
                closed = [
                    monday + timedelta(days=i)
                    for i in range(14)
                    if self.closed_on(monday + timedelta(days=i))
                ]
                self.assertEqual(
                    [d.weekday() for d in closed],
                    [weekday, weekday],
                    "the wrong weekday was closed",
                )

    def test_weekdays_only_skips_the_weekend(self):
        monday = self.today + timedelta(days=(7 - self.today.weekday()) % 7 + 7)
        closures.create_closure(
            self.db, self.admin,
            room_ids=[self.room.id],
            from_date=monday.isoformat(),
            to_date=(monday + timedelta(days=6)).isoformat(),
            start_time="08:00", end_time="10:00",
            weekdays={0, 1, 2, 3, 4},
        )
        closed = [
            (monday + timedelta(days=i)).weekday()
            for i in range(7)
            if self.closed_on(monday + timedelta(days=i))
        ]
        self.assertEqual(closed, [0, 1, 2, 3, 4])


class RuleShapeTests(ClosureTestBase):
    def test_a_single_day_is_one_row_and_a_range_is_also_one_row(self):
        """The point of storing a rule instead of expanding it."""
        self.close(days_ahead=3)
        self.close(days_ahead=10, span_days=47, start="08:00", end="10:00")
        rows = self.query_all("SELECT id FROM room_closures")
        self.assertEqual(len(rows), 2, "a seven-week rule was expanded into rows")

    def test_a_blank_end_date_means_a_single_day(self):
        day = self.today + timedelta(days=3)
        result = closures.create_closure(
            self.db, self.admin, room_ids=[self.room.id],
            from_date=day.isoformat(), to_date="",
            start_time="12:00", end_time="15:00",
        )
        closure = result.closures[0]
        self.assertEqual(closure.from_date, closure.to_date)

    def test_overlapping_closures_are_allowed(self):
        """"Every weekday 08:00-10:00" and "31 Aug 09:00-12:00" will collide
        sooner or later; refusing that would be maddening."""
        self.close(start="08:00", end="10:00")
        self.close(start="09:00", end="12:00")
        self.assertEqual(
            len(self.closed_on(self.today + timedelta(days=3))), 2
        )

    def test_one_submit_can_close_every_room(self):
        second = self.create_room(name="Meeting Room 2")
        day = self.today + timedelta(days=3)
        result = closures.create_closure(
            self.db, self.admin, room_ids=[self.room.id, second.id],
            from_date=day.isoformat(), to_date="",
            start_time="12:00", end_time="15:00",
        )
        self.assertEqual(len(result.closures), 2)

    def test_a_rule_that_could_never_match_is_refused(self):
        """A mask matching no date in the span closes nothing, forever,
        silently."""
        saturday = self.today + timedelta(days=(5 - self.today.weekday()) % 7 + 7)
        with self.assertRaises(AppError) as caught:
            closures.create_closure(
                self.db, self.admin, room_ids=[self.room.id],
                from_date=saturday.isoformat(), to_date=saturday.isoformat(),
                start_time="08:00", end_time="10:00",
                weekdays={0, 1, 2, 3, 4},        # weekdays only, on a Saturday
            )
        self.assertEqual(caught.exception.code, MISSING_FIELD)
        self.assertEqual(caught.exception.details["reason"], "matches_no_dates")

    def test_selecting_no_weekday_is_refused(self):
        day = self.today + timedelta(days=3)
        with self.assertRaises(AppError) as caught:
            closures.create_closure(
                self.db, self.admin, room_ids=[self.room.id],
                from_date=day.isoformat(), to_date="",
                start_time="08:00", end_time="10:00", weekdays=set(),
            )
        self.assertEqual(caught.exception.details["field"], "weekdays")

    def test_an_end_time_before_the_start_is_refused(self):
        day = self.today + timedelta(days=3)
        with self.assertRaises(AppError):
            closures.create_closure(
                self.db, self.admin, room_ids=[self.room.id],
                from_date=day.isoformat(), to_date="",
                start_time="15:00", end_time="12:00",
            )

    def test_an_overlong_reason_is_refused_server_side(self):
        """maxlength only constrains a browser, not a POST, and a long reason
        would wreck the slot row it is rendered into."""
        day = self.today + timedelta(days=3)
        with self.assertRaises(AppError):
            closures.create_closure(
                self.db, self.admin, room_ids=[self.room.id],
                from_date=day.isoformat(), to_date="",
                start_time="12:00", end_time="15:00",
                reason="x" * (closures.MAX_REASON_LENGTH + 1),
            )

    def test_closing_until_midnight_is_expressible(self):
        day = self.today + timedelta(days=3)
        result = closures.create_closure(
            self.db, self.admin, room_ids=[self.room.id],
            from_date=day.isoformat(), to_date="",
            start_time="22:00", end_time="24:00",
        )
        self.assertEqual(result.closures[0].end_minutes, 24 * 60)

    def test_only_an_admin_may_close_a_room(self):
        day = self.today + timedelta(days=3)
        with self.assertRaises(ForbiddenError):
            closures.create_closure(
                self.db, self.member, room_ids=[self.room.id],
                from_date=day.isoformat(), to_date="",
                start_time="12:00", end_time="15:00",
            )


class TaipeiDateTests(ClosureTestBase):
    def test_an_early_morning_booking_uses_its_taipei_date(self):
        """07:00 Taipei is 23:00 UTC the day before. Taking the UTC date would
        test the wrong day's rule and let the booking through.

        The room has to open early for this to be reachable at all: the gap
        between the two dates exists only below 08:00 Taipei, which the
        default opening hour would otherwise refuse first with OUTSIDE_WINDOW.
        """
        self.room = self.create_room(name="Early room", open_minutes=5 * 60)
        day = self.today + timedelta(days=3)
        closures.create_closure(
            self.db, self.admin, room_ids=[self.room.id],
            from_date=day.isoformat(), to_date="",
            start_time="06:00", end_time="09:00",
        )
        start = combine_taipei(day, 7 * 60)
        self.assertEqual(start.astimezone().utcoffset() is None, False)
        self.assertNotEqual(
            start.date(), day, "the fixture no longer exercises the UTC/Taipei gap"
        )
        with self.assertRaises(AppError) as caught:
            preemption.attempt_booking(
                self.db, requester_id=self.member.id, room_id=self.room.id,
                start_at=start, end_at=combine_taipei(day, 8 * 60),
                title="Early", dry_run=True,
            )
        self.assertEqual(caught.exception.code, ROOM_CLOSED)


class AdminExemptionTests(ClosureTestBase):
    def test_an_admin_may_book_over_a_closure(self):
        self.close()
        result = self.attempt(self.admin, 13, 14)
        self.assertEqual(result.outcome, "AVAILABLE")

    def test_a_high_level_member_still_cannot(self):
        """The exemption keys on is_admin, never on level -- which is what
        keeps CLAUDE.md's "admin buys no preemption privilege" rule true from
        the other side."""
        self.close()
        self.assertEqual(self.member.level, 10)
        self.assertEqual(self.admin.level, 1)
        with self.assertRaises(AppError) as caught:
            self.attempt(self.member, 13, 14)
        self.assertEqual(caught.exception.code, ROOM_CLOSED)

    def test_the_error_names_the_hours_and_the_reason(self):
        self.close(reason="Deep clean")
        with self.assertRaises(AppError) as caught:
            self.attempt(self.member, 13, 14)
        details = caught.exception.details
        self.assertEqual(details["start_time"], "12:00")
        self.assertEqual(details["end_time"], "15:00")
        self.assertEqual(details["reason"], "Deep clean")


class ConflictHandshakeTests(ClosureTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.booking = self.create_booking(
            room=self.room, user=self.member,
            start_at=taipei_at(3, 13), end_at=taipei_at(3, 14),
            title="Quarterly review",
        )

    def test_the_first_attempt_blocks_and_lists_what_is_in_the_way(self):
        with self.assertRaises(AppError) as caught:
            self.close()
        exc = caught.exception
        self.assertEqual(exc.code, CONFIRMATION_REQUIRED)
        self.assertEqual(exc.details["future_bookings"], 1)
        listed = exc.details["bookings"]
        self.assertEqual(listed[0]["title"], "Quarterly review")
        self.assertEqual(listed[0]["owner_name"], self.member.full_name)

        self.assertEqual(self.query_all("SELECT id FROM room_closures"), [])
        row = self.query_one(
            "SELECT status FROM bookings WHERE id = ?", (self.booking.id,)
        )
        self.assertEqual(row["status"], CONFIRMED, "it cancelled without asking")

    def test_confirming_cancels_and_closes_in_one_go(self):
        result = self.close(cancel=True)

        self.assertEqual(len(result.closures), 1)
        self.assertEqual(len(result.cancelled), 1)
        row = self.query_one(
            "SELECT status FROM bookings WHERE id = ?", (self.booking.id,)
        )
        self.assertEqual(row["status"], CANCELLED_BY_ADMIN)

    def test_the_displaced_member_is_told_why(self):
        result = self.close(cancel=True)
        self.assertEqual(len(result.emails), 1)
        event = result.emails[0]
        self.assertEqual(event.kind, "E5")
        self.assertEqual(event.to_email, self.member.email)
        self.assertEqual(event.context["reason"], "room_closed")

    def test_a_booking_outside_the_hours_is_not_in_the_way(self):
        self.close(start="08:00", end="10:00")
        self.assertTrue(self.query_all("SELECT id FROM room_closures"))


class PastRangeTests(ClosureTestBase):
    def test_a_range_starting_in_the_past_is_not_deadlocked_by_old_bookings(self):
        """The owner's own example spans dates already gone. A finished
        booking inside it can never be cancelled -- cancel_booking refuses
        with BOOKING_ALREADY_ENDED -- so counting it as a conflict would leave
        the closure permanently unmakeable with no way out of the screen."""
        start = now_utc() - timedelta(days=5)
        self.create_booking(
            room=self.room, user=self.member,
            start_at=start, end_at=start + timedelta(hours=1),
            title="Last week",
        )
        result = closures.create_closure(
            self.db, self.admin, room_ids=[self.room.id],
            from_date=(self.today - timedelta(days=14)).isoformat(),
            to_date=(self.today + timedelta(days=30)).isoformat(),
            start_time="00:00", end_time="24:00",
        )
        self.assertEqual(len(result.closures), 1)
        self.assertEqual(result.cancelled, [])


class DeletionTests(ClosureTestBase):
    def test_deleting_a_closure_reopens_the_slot(self):
        result = self.close()
        closure_id = result.closures[0].id
        with self.assertRaises(AppError):
            self.attempt(self.member, 13, 14)

        closures.delete_closure(self.db, self.admin, closure_id)
        self.assertEqual(self.attempt(self.member, 13, 14).outcome, "AVAILABLE")

    def test_deleting_one_that_is_gone_is_reported(self):
        with self.assertRaises(NotFoundError):
            closures.delete_closure(self.db, self.admin, "no-such-id")

    def test_a_member_cannot_delete_a_closure(self):
        result = self.close()
        with self.assertRaises(ForbiddenError):
            closures.delete_closure(self.db, self.member, result.closures[0].id)

    def test_deleting_a_room_takes_its_closures_with_it(self):
        """The room_id foreign key is enforced on both backends, so without
        this the DELETE raises a raw integrity error rather than an AppError
        -- a 500 instead of a message."""
        self.close()
        rooms.delete_room(self.db, self.admin, self.room.id)
        self.assertEqual(self.query_all("SELECT id FROM room_closures"), [])

    def test_deactivating_a_room_keeps_its_closures(self):
        """Reactivating must restore the room as it was."""
        self.close()
        rooms.set_active(self.db, self.admin, self.room.id, False)
        self.assertEqual(len(self.query_all("SELECT id FROM room_closures")), 1)


class ListingTests(ClosureTestBase):
    def test_finished_closures_are_hidden_but_not_deleted(self):
        day = self.today - timedelta(days=10)
        closures.create_closure(
            self.db, self.admin, room_ids=[self.room.id],
            from_date=day.isoformat(), to_date=day.isoformat(),
            start_time="12:00", end_time="15:00",
        )
        self.assertEqual(closures.list_closures(self.db), [])
        self.assertEqual(len(closures.list_closures(self.db, include_past=True)), 1)
        self.assertEqual(
            len(self.query_all("SELECT id FROM room_closures")), 1,
            "a finished closure was deleted rather than hidden",
        )


class ValidationOrderTests(ClosureTestBase):
    """Where the closure check sits among the §6.5 rules.

    The acceptance tests assert on the *first* failure, so placement is
    behaviour. This pins it as an order rather than rule by rule: each case
    violates its own rule and the ones after it, so moving a check produces a
    failure that names the position.
    """

    def test_a_closure_is_reported_before_the_quota(self):
        """A closure is a fact about the room; the quota is a fact about you.
        Naming the quota for a slot nobody can have is the wrong first answer,
        and it leaks that the request was otherwise fine."""
        settings = self.db.run_in_transaction(Settings.load)
        quota = settings.quota_for(self.member.level)
        for i in range(quota or 1):
            self.create_booking(
                room=self.room, user=self.member,
                start_at=taipei_at(20 + i, 9), end_at=taipei_at(20 + i, 10),
            )
        self.close()
        with self.assertRaises(AppError) as caught:
            self.attempt(self.member, 13, 14)
        self.assertEqual(caught.exception.code, ROOM_CLOSED)

    def test_the_room_window_is_reported_before_a_closure(self):
        """Operating hours are permanent; a closure is an overlay on hours
        that must exist first. Saying "closed 06:00-09:00" for a room that
        never opens before 08:00 sends the member to fix the wrong end."""
        self.close(start="06:00", end="09:00")
        with self.assertRaises(AppError) as caught:
            self.attempt(self.member, 6, 7)
        self.assertEqual(caught.exception.code, "OUTSIDE_WINDOW")

    def test_an_inactive_room_is_reported_before_a_closure(self):
        self.close()
        rooms.set_active(self.db, self.admin, self.room.id, False)
        with self.assertRaises(AppError) as caught:
            self.attempt(self.member, 13, 14)
        self.assertEqual(caught.exception.code, "ROOM_INACTIVE")


class ClosedSlotIsNotContestedTests(ClosureTestBase):
    def test_a_closed_slot_never_offers_a_victim_to_preempt(self):
        """Validation runs before the overlap scan, so a member is never
        invited to displace someone for a slot they cannot have anyway."""
        self.create_booking(
            room=self.room, user=self.admin,
            start_at=taipei_at(3, 13), end_at=taipei_at(3, 14),
        )
        self.close(cancel=True)
        # The admin's booking was cancelled by the closure; re-make it, which
        # an admin is allowed to do.
        self.create_booking(
            room=self.room, user=self.admin,
            start_at=taipei_at(3, 13), end_at=taipei_at(3, 14),
        )
        with self.assertRaises(AppError) as caught:
            self.attempt(self.member, 13, 14)
        self.assertEqual(caught.exception.code, ROOM_CLOSED)


class AdminScreenTests(ClosureTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.app = create_app_for(self)
        self.client = Client(self.app)
        self.client.get("/login")
        self.client.post(
            "/login", form={"email": "admin@example.com", "password": _PASSWORD}
        )

    def form(self, **overrides):
        day = self.today + timedelta(days=3)
        payload = {
            "room_id": self.room.id,
            "from_date": day.isoformat(),
            "to_date": "",
            "start_time": "12:00",
            "end_time": "15:00",
            "reason": "Deep clean",
            **{f"weekday_{i}": "on" for i in range(7)},
        }
        payload.update(overrides)
        return payload

    def test_the_page_is_reachable_from_the_admin_nav(self):
        page = self.client.get("/admin")
        self.assertIn('href="/admin/closures"', page.text)

    def test_all_seven_weekday_boxes_are_actually_read(self):
        """Seven inputs called "weekday" would collapse to one: the form
        parser keeps only the first value of a repeated name."""
        response = self.client.post(
            "/admin/closures", form=self.form(**{"weekday_5": "", "weekday_6": ""})
        )
        self.assertEqual(response.status, 303)
        closure = closures.list_closures(self.db)[0]
        self.assertEqual(closure.weekdays, [0, 1, 2, 3, 4])

    def test_creating_and_deleting_through_the_screen(self):
        self.client.post("/admin/closures", form=self.form())
        listed = closures.list_closures(self.db)
        self.assertEqual(len(listed), 1)

        page = self.client.get("/admin/closures")
        self.assertIn("Deep clean", page.text)

        response = self.client.post(
            f"/admin/closures/{listed[0].id}/delete", form={}
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(closures.list_closures(self.db), [])

    def test_the_conflict_page_lists_the_bookings_in_the_way(self):
        self.create_booking(
            room=self.room, user=self.member,
            start_at=taipei_at(3, 13), end_at=taipei_at(3, 14),
            title="Quarterly review",
        )
        page = self.client.post("/admin/closures", form=self.form())

        self.assertEqual(page.status, 200, "it redirected instead of asking")
        self.assertIn("Quarterly review", page.text)
        self.assertIn(self.member.full_name, page.text)
        self.assertIn('name="confirm_cancel"', page.text)
        self.assertEqual(closures.list_closures(self.db), [])

    def test_confirming_from_that_page_creates_the_same_rule(self):
        self.create_booking(
            room=self.room, user=self.member,
            start_at=taipei_at(3, 13), end_at=taipei_at(3, 14),
        )
        self.client.post("/admin/closures", form=self.form())
        response = self.client.post(
            "/admin/closures", form=self.form(confirm_cancel="1")
        )
        self.assertEqual(response.status, 303)
        closure = closures.list_closures(self.db)[0]
        self.assertEqual(closure.start_minutes, 12 * 60)
        self.assertEqual(closure.end_minutes, 15 * 60)

    def test_a_member_cannot_reach_the_page(self):
        member_client = Client(self.app)
        member_client.get("/login")
        member_client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )
        response = member_client.get("/admin/closures")
        self.assertIn(response.status, (303, 403))


def create_app_for(case) -> object:
    from app.web.app import create_app

    return create_app(
        case.db, Config(base_url="http://testserver", email_transport="fake")
    )


class MemberGridTests(ClosureTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.app = create_app_for(self)

    def grid(self, email: str) -> str:
        client = Client(self.app)
        client.get("/login")
        client.post("/login", form={"email": email, "password": _PASSWORD})
        day = self.today + timedelta(days=3)
        return client.get(f"/day?date={day.isoformat()}").text

    def test_a_member_sees_the_slot_closed_and_cannot_click_it(self):
        self.close(reason="Deep clean")
        html = self.grid("member@example.com")
        self.assertIn("is-closed", html)
        self.assertIn("Deep clean", html)

    def test_an_admin_sees_it_closed_but_may_still_book_it(self):
        self.close(reason="Deep clean")
        member_html = self.grid("member@example.com")
        admin_html = self.grid("admin@example.com")
        self.assertIn("is-closed", admin_html)
        self.assertGreater(
            admin_html.count("slot-action"),
            member_html.count("slot-action"),
            "the admin lost actions a member did not have",
        )

    def test_the_week_view_shows_closures_too(self):
        """Both views funnel through the same slot renderer."""
        self.close()
        client = Client(self.app)
        client.get("/login")
        client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )
        day = self.today + timedelta(days=3)
        html = client.get(f"/week?room={self.room.id}&date={day.isoformat()}").text
        self.assertIn("is-closed", html)

    def test_the_api_reports_closures(self):
        self.close(reason="Deep clean")
        client = Client(self.app)
        client.get("/login")
        client.post(
            "/login", form={"email": "member@example.com", "password": _PASSWORD}
        )
        day = self.today + timedelta(days=3)
        payload = client.get(f"/api/availability?date={day.isoformat()}").json()
        entry = payload["rooms"][0]["closures"][0]
        self.assertEqual(entry["start"], "12:00")
        self.assertEqual(entry["end"], "15:00")
        self.assertEqual(entry["reason"], "Deep clean")
