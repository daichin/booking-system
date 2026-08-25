"""Rooms and booking basics (spec FR-4, FR-5, acceptance group B)."""

from __future__ import annotations

from app.errors import (
    AppError,
    BEYOND_HORIZON,
    BOOKING_ALREADY_ENDED,
    CONFIRMATION_REQUIRED,
    CROSSES_MIDNIGHT,
    END_NOT_AFTER_START,
    ForbiddenError,
    NOT_ACTIVE,
    NOT_BOOKING_OWNER,
    OFF_GRID,
    OUTSIDE_WINDOW,
    QUOTA_EXCEEDED,
    ROOM_HAS_BOOKINGS,
    ROOM_INACTIVE,
    START_IN_PAST,
    TITLE_REQUIRED,
    TOO_LONG,
)
from app.models import (
    CANCELLED_BY_ADMIN,
    CANCELLED_BY_USER,
    CONFIRMED,
    PENDING_APPROVAL,
)
from app.services import bookings, rooms
from app.services.preemption import attempt_booking
from app.timeutil import local_date
from tests.support import AppTestCase, taipei_at


class BookingTestBase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A fixed morning "now" keeps every relative rule deterministic.
        self.freeze_at(0, 9, 0)
        self.room = self.create_room(name="會議室 A")
        self.user = self.create_user(level=1)

    def book(self, *, user=None, start=None, end=None, title="週會", room=None,
             confirm=False, dry_run=False):
        return attempt_booking(
            self.db,
            requester_id=(user or self.user).id,
            room_id=(room or self.room).id,
            start_at=start or taipei_at(1, 14),
            end_at=end or taipei_at(1, 15),
            title=title,
            confirm_preemption=confirm,
            dry_run=dry_run,
        )


class ValidationTests(BookingTestBase):
    def test_b1_aligned_booking_succeeds(self):
        result = self.book(start=taipei_at(1, 14), end=taipei_at(1, 15, 30))
        self.assertTrue(result.created)
        self.assertEqual(result.booking.status, CONFIRMED)

    def test_b1_off_grid_booking_is_rejected(self):
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(1, 14, 10), end=taipei_at(1, 15))
        self.assertEqual(ctx.exception.code, OFF_GRID)

    def test_b2_booking_longer_than_the_maximum_is_rejected(self):
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(1, 9), end=taipei_at(1, 14, 30))  # 5.5h > 4h
        self.assertEqual(ctx.exception.code, TOO_LONG)

    def test_b3_booking_beyond_the_horizon_is_rejected(self):
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(61, 14), end=taipei_at(61, 15))
        self.assertEqual(ctx.exception.code, BEYOND_HORIZON)

    def test_b3_booking_at_the_horizon_edge_is_allowed(self):
        result = self.book(start=taipei_at(60, 14), end=taipei_at(60, 15))
        self.assertTrue(result.created)

    def test_b4_booking_outside_the_room_window_is_rejected(self):
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(1, 7), end=taipei_at(1, 8))
        self.assertEqual(ctx.exception.code, OUTSIDE_WINDOW)

    def test_b4_per_room_window_overrides_the_default(self):
        late = self.create_room(name="會議室 B", open_minutes=9 * 60, close_minutes=12 * 60)
        with self.assertRaises(AppError) as ctx:
            self.book(room=late, start=taipei_at(1, 13), end=taipei_at(1, 14))
        self.assertEqual(ctx.exception.code, OUTSIDE_WINDOW)
        self.assertTrue(self.book(room=late, start=taipei_at(1, 10),
                                  end=taipei_at(1, 11)).created)

    def test_end_must_be_after_start(self):
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(1, 15), end=taipei_at(1, 14))
        self.assertEqual(ctx.exception.code, END_NOT_AFTER_START)

    def test_start_must_be_in_the_future(self):
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(0, 8), end=taipei_at(0, 8, 30))
        self.assertEqual(ctx.exception.code, START_IN_PAST)

    def test_booking_may_not_cross_midnight(self):
        allnight = self.create_room(name="全天", open_minutes=0, close_minutes=24 * 60)
        with self.assertRaises(AppError) as ctx:
            self.book(room=allnight, start=taipei_at(1, 23), end=taipei_at(2, 1))
        self.assertEqual(ctx.exception.code, CROSSES_MIDNIGHT)

    def test_booking_ending_exactly_at_midnight_is_allowed(self):
        allnight = self.create_room(name="全天", open_minutes=0, close_minutes=24 * 60)
        self.assertTrue(
            self.book(room=allnight, start=taipei_at(1, 23), end=taipei_at(2, 0)).created
        )

    def test_inactive_room_is_rejected(self):
        closed = self.create_room(name="停用室", is_active=False)
        with self.assertRaises(AppError) as ctx:
            self.book(room=closed)
        self.assertEqual(ctx.exception.code, ROOM_INACTIVE)

    def test_pending_member_cannot_book(self):
        pending = self.create_user(status=PENDING_APPROVAL)
        with self.assertRaises(ForbiddenError) as ctx:
            self.book(user=pending)
        self.assertEqual(ctx.exception.code, NOT_ACTIVE)

    def test_title_is_required(self):
        with self.assertRaises(AppError) as ctx:
            self.book(title="   ")
        self.assertEqual(ctx.exception.code, TITLE_REQUIRED)

    def test_validation_runs_in_the_spec_order(self):
        # Both off-grid and too-long: the spec checks alignment (step 3)
        # before duration (step 4), so alignment must be what is reported.
        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(1, 9, 10), end=taipei_at(1, 20, 10))
        self.assertEqual(ctx.exception.code, OFF_GRID)


class QuotaTests(BookingTestBase):
    def test_b5_quota_blocks_the_fourth_booking_and_cancelling_frees_it(self):
        self.assertEqual(self.settings().quota_for(1), 3)
        made = [
            self.book(start=taipei_at(day, 14), end=taipei_at(day, 15)).booking
            for day in (1, 2, 3)
        ]

        with self.assertRaises(AppError) as ctx:
            self.book(start=taipei_at(4, 14), end=taipei_at(4, 15))
        self.assertEqual(ctx.exception.code, QUOTA_EXCEEDED)

        bookings.cancel_booking(self.db, actor=self.user, booking_id=made[0].id)
        self.assertTrue(self.book(start=taipei_at(4, 14), end=taipei_at(4, 15)).created)

    def test_quota_counts_only_future_confirmed_bookings(self):
        past_room = self.create_room(name="舊室")
        self.create_booking(
            room=past_room, user=self.user,
            start_at=taipei_at(-2, 14), end_at=taipei_at(-2, 15),
        )
        for day in (1, 2, 3):
            self.book(start=taipei_at(day, 14), end=taipei_at(day, 15))
        # The historical booking must not count towards the quota of 3.
        with self.assertRaises(AppError):
            self.book(start=taipei_at(4, 14), end=taipei_at(4, 15))

    def test_zero_quota_means_unlimited(self):
        quotas = dict(self.settings().values["quota_by_level"])
        quotas["1"] = 0
        self.set_setting("quota_by_level", quotas)
        for day in range(1, 6):
            self.assertTrue(
                self.book(start=taipei_at(day, 14), end=taipei_at(day, 15)).created
            )


class CancellationTests(BookingTestBase):
    def test_b6_cancelling_frees_the_slot_immediately(self):
        first = self.book().booking
        other = self.create_user(level=1)

        # While it stands, an equal-level member is blocked.
        blocked = self.book(user=other)
        self.assertEqual(blocked.outcome, "BLOCKED")

        bookings.cancel_booking(self.db, actor=self.user, booking_id=first.id)
        self.assertTrue(self.book(user=other).created)

    def test_owner_cancellation_sends_e6(self):
        booking = self.book().booking
        result = bookings.cancel_booking(
            self.db, actor=self.user, booking_id=booking.id
        )
        self.assertEqual(result.booking.status, CANCELLED_BY_USER)
        self.assertEqual([event.kind for event in result.emails], ["E6"])

    def test_admin_cancellation_sends_e5_to_the_owner(self):
        booking = self.book().booking
        admin = self.create_user(is_admin=True, level=1)
        result = bookings.cancel_booking(
            self.db, actor=admin, booking_id=booking.id
        )
        self.assertEqual(result.booking.status, CANCELLED_BY_ADMIN)
        self.assertEqual([event.kind for event in result.emails], ["E5"])
        self.assertEqual(result.emails[0].to_email, self.user.email)

    def test_a_stranger_cannot_cancel_someone_elses_booking(self):
        booking = self.book().booking
        stranger = self.create_user()
        with self.assertRaises(ForbiddenError) as ctx:
            bookings.cancel_booking(self.db, actor=stranger, booking_id=booking.id)
        self.assertEqual(ctx.exception.code, NOT_BOOKING_OWNER)

    def test_a_finished_booking_cannot_be_cancelled(self):
        finished = self.create_booking(
            room=self.room, user=self.user,
            start_at=taipei_at(-1, 14), end_at=taipei_at(-1, 15),
        )
        with self.assertRaises(AppError) as ctx:
            bookings.cancel_booking(self.db, actor=self.user, booking_id=finished.id)
        self.assertEqual(ctx.exception.code, BOOKING_ALREADY_ENDED)

    def test_my_bookings_splits_upcoming_from_history(self):
        upcoming = self.book().booking
        self.create_booking(
            room=self.room, user=self.user,
            start_at=taipei_at(-3, 14), end_at=taipei_at(-3, 15),
        )
        ahead, past = bookings.list_for_user(self.db, self.user.id)
        self.assertEqual([row["id"] for row in ahead], [upcoming.id])
        self.assertEqual(len(past), 1)


class RoomAdminTests(BookingTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.create_user(is_admin=True, full_name="管理員")

    def test_only_admins_may_manage_rooms(self):
        with self.assertRaises(ForbiddenError):
            rooms.create_room(self.db, self.user, name="偷偷建立")

    def test_create_and_update_a_room(self):
        room = rooms.create_room(
            self.db, self.admin, name="大會議室", capacity=20, location="5 樓",
            open_time="09:00", close_time="18:00",
        )
        self.assertEqual(room.capacity, 20)
        self.assertEqual(room.open_minutes, 9 * 60)

        updated = rooms.update_room(self.db, self.admin, room.id, capacity=30)
        self.assertEqual(updated.capacity, 30)
        self.assertEqual(updated.name, "大會議室")

    def test_close_time_must_follow_open_time(self):
        with self.assertRaises(AppError):
            rooms.create_room(
                self.db, self.admin, name="錯誤", open_time="18:00", close_time="09:00"
            )

    def test_deactivating_a_room_with_bookings_needs_confirmation(self):
        self.book()
        with self.assertRaises(AppError) as ctx:
            rooms.set_active(self.db, self.admin, self.room.id, False)
        self.assertEqual(ctx.exception.code, CONFIRMATION_REQUIRED)
        self.assertEqual(ctx.exception.details["future_bookings"], 1)
        # Nothing may have changed.
        self.assertTrue(self.get_room(self.room.id).is_active)

    def test_confirmed_deactivation_cancels_bookings_and_emails_owners(self):
        booking = self.book().booking
        result = rooms.set_active(
            self.db, self.admin, self.room.id, False, cancel_bookings=True
        )
        self.assertFalse(result.room.is_active)
        self.assertEqual(len(result.cancelled), 1)
        self.assertEqual(self.get_booking(booking.id).status, CANCELLED_BY_ADMIN)
        self.assertEqual([event.kind for event in result.emails], ["E5"])

    def test_deactivating_an_empty_room_needs_no_confirmation(self):
        result = rooms.set_active(self.db, self.admin, self.room.id, False)
        self.assertFalse(result.room.is_active)
        self.assertEqual(result.cancelled, [])

    def test_a_room_with_history_cannot_be_deleted(self):
        self.book()
        with self.assertRaises(AppError) as ctx:
            rooms.delete_room(self.db, self.admin, self.room.id)
        self.assertEqual(ctx.exception.code, ROOM_HAS_BOOKINGS)

    def test_inactive_rooms_are_hidden_from_the_default_listing(self):
        rooms.set_active(self.db, self.admin, self.room.id, False)
        self.assertEqual(rooms.list_rooms(self.db), [])
        self.assertEqual(len(rooms.list_rooms(self.db, include_inactive=True)), 1)

    def test_availability_reports_confirmed_bookings_without_emails(self):
        self.book(start=taipei_at(1, 14), end=taipei_at(1, 15))
        day = rooms.availability(self.db, day=local_date(taipei_at(1, 12)))
        # Resolve the room we care about regardless of ordering.
        entry = next(item for item in day if item.room.id == self.room.id)
        self.assertEqual(len(entry.bookings), 1)
        self.assertNotIn("email", entry.bookings[0]["owner"])
        self.assertEqual(entry.open_minutes, 8 * 60)
