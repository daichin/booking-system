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
from typing import Callable

UTC = timezone.utc
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")

#: Weekday characters used in zh-TW date rendering, Monday first.
_WEEKDAY_ZH = "一二三四五六日"

#: Canonical serialisation used by the SQLite backend.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


#: Overridable clock. Production leaves this ``None``; tests install a fixed
#: clock so that rules expressed relative to "now" -- the preemption
#: protection window above all -- are deterministic instead of depending on
#: what time of day the suite happens to run.
_clock: "Callable[[], datetime] | None" = None


def set_clock(clock: "Callable[[], datetime] | None") -> None:
    """Install (or clear, with ``None``) the clock used by :func:`now_utc`."""
    global _clock
    _clock = clock


def now_utc() -> datetime:
    """Current time as an aware UTC datetime.

    Every module reads the clock through this function, so installing a test
    clock affects the whole application even where the name was imported
    directly.
    """
    if _clock is not None:
        return ensure_utc(_clock())
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


# --- locale-aware rendering --------------------------------------------------
#
# Fixed tables rather than the `locale` module, whose behaviour depends on
# what the host has installed -- not something to leave to a container image.

_WEEKDAY_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_EN = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

#: The timezone label every rendered time carries (spec §9.2).
_TZ_LABEL = {"zh-TW": "台北時間", "en": "Taipei time"}


def format_date(value: datetime, locale: str = "zh-TW") -> str:
    """A date in the reader's language, always in Taipei time."""
    if locale == "en":
        local = to_taipei(value)
        return (
            f"{_WEEKDAY_EN[local.weekday()]} {local.day} "
            f"{_MONTH_EN[local.month - 1]} {local.year}"
        )
    return format_date_zh(value)


def format_time(value: datetime, locale: str = "zh-TW") -> str:
    """24-hour wall clock; the same in both languages."""
    return format_time_zh(value)


def format_range(start: datetime, end: datetime, locale: str = "zh-TW") -> str:
    """Render a booking window in the reader's language.

    zh-TW: ``2026-09-03 (四) 14:00–15:00 (台北時間)``
    en:    ``Thu 3 Sep 2026, 14:00–15:00 (Taipei time)``
    """
    if locale != "en":
        return format_range_zh(start, end)

    label = _TZ_LABEL["en"]
    if to_taipei(start).date() == to_taipei(end).date():
        return (
            f"{format_date(start, 'en')}, "
            f"{format_time_zh(start)}–{format_time_zh(end)} ({label})"
        )
    return (
        f"{format_date(start, 'en')} {format_time_zh(start)} – "
        f"{format_date(end, 'en')} {format_time_zh(end)} ({label})"
    )
