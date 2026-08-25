"""Time handling.

Spec C8: all timestamps are stored in UTC and displayed in Asia/Taipei.

Taiwan has observed a fixed UTC+08:00 with no daylight saving since 1979, so a
fixed-offset timezone is used rather than ``zoneinfo``. This keeps the app
dependency-free on Windows, where ``zoneinfo`` needs the external ``tzdata``
package to resolve ``Asia/Taipei``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")

#: Weekday characters used in zh-TW date rendering, Monday first.
_WEEKDAY_ZH = "一二三四五六日"

#: Canonical serialisation used by the SQLite backend.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


def now_utc() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Naive datetimes are assumed to already be UTC; this is the only place that
    assumption is made, so that storage layers never silently guess.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_taipei(value: datetime) -> datetime:
    """Convert an instant to Asia/Taipei for display."""
    return ensure_utc(value).astimezone(TAIPEI)


def isoformat_utc(value: datetime) -> str:
    """Serialise to the canonical UTC string stored by the SQLite backend."""
    return ensure_utc(value).isoformat(timespec="microseconds")


def looks_like_timestamp(value: str) -> bool:
    """Whether a string is one of our canonical timestamp serialisations."""
    return bool(_ISO_RE.match(value))


def parse_utc(value: str) -> datetime:
    """Parse a canonical timestamp string back into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


def is_aligned(value: datetime, slot_minutes: int) -> bool:
    """Whether an instant lands on a slot boundary (spec FR-5 step 3).

    Alignment is evaluated in Taipei local time because the booking grid is
    what members see. With a whole-hour offset the result matches UTC, but
    stating it explicitly keeps the rule correct if the offset ever changes.
    """
    local = to_taipei(value)
    if local.second or local.microsecond:
        return False
    return (local.hour * 60 + local.minute) % slot_minutes == 0


def local_date(value: datetime) -> date:
    """The Taipei calendar date an instant falls on."""
    return to_taipei(value).date()


def minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes from ``start`` to ``end``."""
    return int((ensure_utc(end) - ensure_utc(start)).total_seconds() // 60)


def taipei_midnight(day: date) -> datetime:
    """The UTC instant at which a Taipei calendar day begins."""
    return datetime(day.year, day.month, day.day, tzinfo=TAIPEI).astimezone(UTC)


def combine_taipei(day: date, minutes_from_midnight: int) -> datetime:
    """Build a UTC instant from a Taipei date plus minutes past local midnight."""
    return taipei_midnight(day) + timedelta(minutes=minutes_from_midnight)


def minutes_since_midnight(value: datetime) -> int:
    """Minutes past Taipei midnight for an instant."""
    local = to_taipei(value)
    return local.hour * 60 + local.minute


def parse_hhmm(value: str) -> int:
    """Parse ``"08:00"`` into minutes past midnight.

    ``"24:00"`` is accepted as end-of-day so a room can stay open until
    midnight without a booking crossing the date boundary.
    """
    text = value.strip()
    hours, _, minutes = text.partition(":")
    hh, mm = int(hours), int(minutes)
    if not (0 <= hh <= 24 and 0 <= mm < 60) or (hh == 24 and mm != 0):
        raise ValueError(f"invalid time of day: {value!r}")
    return hh * 60 + mm


def format_hhmm(minutes_from_midnight: int) -> str:
    """Inverse of :func:`parse_hhmm`."""
    return f"{minutes_from_midnight // 60:02d}:{minutes_from_midnight % 60:02d}"


def format_date_zh(value: datetime) -> str:
    """Render a date as ``2026-09-03 (三)`` in Taipei time."""
    local = to_taipei(value)
    return f"{local:%Y-%m-%d} ({_WEEKDAY_ZH[local.weekday()]})"


def format_time_zh(value: datetime) -> str:
    """Render a wall-clock time as ``14:00`` in Taipei time."""
    return f"{to_taipei(value):%H:%M}"


def format_range_zh(start: datetime, end: datetime) -> str:
    """Render a booking window for emails and the UI (spec §9.2).

    Always carries an explicit timezone label, e.g.
    ``2026-09-03 (三) 14:00–15:00 (台北時間)``. A booking that somehow spans a
    date boundary renders both dates rather than silently hiding the second.
    """
    start_local, end_local = to_taipei(start), to_taipei(end)
    if start_local.date() == end_local.date():
        return (
            f"{format_date_zh(start)} {format_time_zh(start)}"
            f"–{format_time_zh(end)} (台北時間)"
        )
    return (
        f"{format_date_zh(start)} {format_time_zh(start)}–"
        f"{format_date_zh(end)} {format_time_zh(end)} (台北時間)"
    )
