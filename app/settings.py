"""System settings (spec §5).

Every tunable business rule lives in the ``settings`` table and is read from
there at runtime. The values below are seeds for a fresh database only -- no
module may import them as constants, because an admin can change any of them
while the app is running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.db.base import Connection
from app.errors import AppError, INVALID_SETTING
from app.timeutil import format_hhmm, now_utc, parse_hhmm

#: Spec §5 defaults, seeded on first deploy.
#:
#: ``quota_by_level`` must cover levels 1-10; the spec gives the endpoints
#: (3 at level 1, 20 at level 10) and an ellipsis, so the intermediate values
#: are a smooth ramp between them. All of it is admin-editable.
DEFAULTS: dict[str, Any] = {
    "slot_minutes": 30,
    "max_booking_minutes": 240,
    "booking_horizon_days": 60,
    "default_open_time": "08:00",
    "default_close_time": "22:00",
    "preemption_protection_minutes": 120,
    "quota_by_level": {
        "1": 3, "2": 3, "3": 5, "4": 5, "5": 8,
        "6": 8, "7": 10, "8": 12, "9": 15, "10": 20,
    },
    "reminder_lead_minutes": 60,
    "reminders_enabled": True,
    "verify_token_hours": 24,
    "invite_token_hours": 168,
    "reset_token_hours": 2,
    "daily_email_cap": 280,
    # Spec §9.4 says three. It is a setting rather than a constant because
    # how many attempts are worth making depends on how reliable the mail
    # provider turns out to be, which cannot be known from here -- and the
    # cost of giving up too early is a member never learning their booking
    # was cancelled.
    "email_max_attempts": 3,
    # Not a business rule from §5, but the same reasoning applies: which
    # meeting titles are common differs per organisation, so it belongs in
    # the admin-editable settings rather than in the code. Shipped in the
    # default language; an admin editing them is the expected first step,
    # and once seeded the stored value is what everyone sees.
    "title_presets": [
        "Team meeting", "Weekly sync", "Project discussion",
        "Interview", "Client visit", "One-on-one",
    ],
}

#: Validation rules applied whenever an admin edits a setting.
_INT_RANGES = {
    "slot_minutes": (5, 240),
    "max_booking_minutes": (5, 1440),
    "booking_horizon_days": (1, 730),
    "preemption_protection_minutes": (0, 10080),
    "reminder_lead_minutes": (0, 1440),
    "verify_token_hours": (1, 8760),
    "invite_token_hours": (1, 8760),
    "reset_token_hours": (1, 8760),
    "daily_email_cap": (0, 100000),
    "email_max_attempts": (1, 20),
}


@dataclass(frozen=True)
class Settings:
    """An immutable snapshot of the settings table."""

    values: dict[str, Any]

    @classmethod
    def load(cls, conn: Connection) -> "Settings":
        rows = conn.query_all("SELECT key, value FROM settings")
        stored = {row["key"]: json.loads(row["value"]) for row in rows}
        # Fall back to defaults for keys a future migration adds but an
        # existing database has not been seeded with yet.
        return cls({**DEFAULTS, **stored})

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    # --- typed accessors ---------------------------------------------------

    @property
    def slot_minutes(self) -> int:
        return int(self.values["slot_minutes"])

    @property
    def max_booking_minutes(self) -> int:
        return int(self.values["max_booking_minutes"])

    @property
    def booking_horizon_days(self) -> int:
        return int(self.values["booking_horizon_days"])

    @property
    def default_open_minutes(self) -> int:
        return parse_hhmm(str(self.values["default_open_time"]))

    @property
    def default_close_minutes(self) -> int:
        return parse_hhmm(str(self.values["default_close_time"]))

    @property
    def preemption_protection_minutes(self) -> int:
        return int(self.values["preemption_protection_minutes"])

    @property
    def reminder_lead_minutes(self) -> int:
        return int(self.values["reminder_lead_minutes"])

    @property
    def reminders_enabled(self) -> bool:
        return bool(self.values["reminders_enabled"])

    @property
    def verify_token_hours(self) -> int:
        return int(self.values["verify_token_hours"])

    @property
    def invite_token_hours(self) -> int:
        return int(self.values["invite_token_hours"])

    @property
    def reset_token_hours(self) -> int:
        return int(self.values["reset_token_hours"])

    @property
    def daily_email_cap(self) -> int:
        return int(self.values["daily_email_cap"])

    @property
    def email_max_attempts(self) -> int:
        return int(self.values["email_max_attempts"])

    @property
    def title_presets(self) -> list[str]:
        """One-click meeting titles offered on the booking form."""
        raw = self.values.get("title_presets") or []
        return [str(item) for item in raw if str(item).strip()]

    def quota_for(self, level: int) -> int | None:
        """Simultaneous future confirmed bookings allowed at ``level``.

        ``None`` means unlimited, which spec §5 assigns to both ``0`` and a
        null entry.
        """
        quotas = self.values.get("quota_by_level") or {}
        raw = quotas.get(str(level), quotas.get(level))
        if raw in (None, 0):
            return None
        return int(raw)


def seed_defaults(conn: Connection) -> int:
    """Insert any missing default settings. Idempotent (spec §12 E4)."""
    existing = {row["key"] for row in conn.query_all("SELECT key FROM settings")}
    now = now_utc()
    inserted = 0
    for key, value in DEFAULTS.items():
        if key in existing:
            continue
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), now),
        )
        inserted += 1
    return inserted


def coerce(key: str, raw: Any) -> Any:
    """Validate and normalise an admin-supplied setting value.

    Raises :class:`AppError` with ``INVALID_SETTING`` rather than letting a bad
    value reach the database, because these values drive booking rules for
    everyone.
    """
    if key not in DEFAULTS:
        raise AppError(INVALID_SETTING, {"key": key, "reason": "unknown_key"})

    if key in _INT_RANGES:
        low, high = _INT_RANGES[key]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise AppError(INVALID_SETTING, {"key": key, "reason": "not_an_integer"})
        if not low <= value <= high:
            raise AppError(
                INVALID_SETTING,
                {"key": key, "reason": "out_of_range", "min": low, "max": high},
            )
        return value

    if key in ("default_open_time", "default_close_time"):
        try:
            return format_hhmm(parse_hhmm(str(raw)))
        except ValueError:
            raise AppError(INVALID_SETTING, {"key": key, "reason": "not_a_time"})

    if key == "reminders_enabled":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    if key == "title_presets":
        value = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("[") \
            else raw
        if isinstance(value, str):
            # An admin editing a textarea, one title per line.
            value = [line.strip() for line in value.splitlines()]
        if not isinstance(value, list):
            raise AppError(INVALID_SETTING, {"key": key, "reason": "not_a_list"})
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if len(cleaned) > 20:
            raise AppError(INVALID_SETTING, {"key": key, "reason": "too_many"})
        return cleaned

    if key == "quota_by_level":
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise AppError(INVALID_SETTING, {"key": key, "reason": "not_an_object"})
        quotas: dict[str, int] = {}
        for level in range(1, 11):
            if str(level) not in value:
                raise AppError(
                    INVALID_SETTING,
                    {"key": key, "reason": "missing_level", "level": level},
                )
            entry = value[str(level)]
            if entry is None:
                quotas[str(level)] = 0  # 0 and null both mean unlimited
                continue
            try:
                quota = int(entry)
            except (TypeError, ValueError):
                raise AppError(
                    INVALID_SETTING,
                    {"key": key, "reason": "not_an_integer", "level": level},
                )
            if quota < 0:
                raise AppError(
                    INVALID_SETTING, {"key": key, "reason": "negative", "level": level}
                )
            quotas[str(level)] = quota
        return quotas

    return raw


def update(conn: Connection, key: str, raw: Any, actor_id: str | None = None) -> Any:
    """Validate and persist one setting. Returns the stored value."""
    value = coerce(key, raw)
    changed = conn.execute(
        "UPDATE settings SET value = ?, updated_at = ?, updated_by = ? WHERE key = ?",
        (json.dumps(value), now_utc(), actor_id, key),
    ).rowcount
    if not changed:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)",
            (key, json.dumps(value), now_utc(), actor_id),
        )
    return value
