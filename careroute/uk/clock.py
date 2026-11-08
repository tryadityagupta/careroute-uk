"""uk/clock.py — 'now', injectable so tests (and ages, slot dates) are stable."""

from __future__ import annotations

from datetime import date, datetime, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().date()


class FixedClock(Clock):
    def __init__(self, when: datetime):
        self._when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._when


def age_on(dob: str, today: date) -> int:
    born = date.fromisoformat(dob)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
