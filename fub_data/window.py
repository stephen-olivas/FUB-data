"""Date-window helpers. FUB has no created-date filter on /people, so we walk
people newest-first and filter on `created` locally."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Window:
    start: datetime  # inclusive, tz-aware
    end: datetime  # exclusive, tz-aware

    @property
    def label(self) -> str:
        last_day = (self.end - timedelta(seconds=1)).date()
        return f"{self.start.date().isoformat()}_to_{last_day.isoformat()}"

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def build_window(
    start: str | None = None,
    end: str | None = None,
    days: int = 7,
    tz: str = "America/Los_Angeles",
    today: date | None = None,
) -> Window:
    """start/end are YYYY-MM-DD local dates; end is inclusive.

    No start given -> the last complete Monday-Sunday week.
    Start only -> start + `days` days.
    """
    zone = ZoneInfo(tz)
    if start:
        start_d = date.fromisoformat(start)
    else:
        today = today or datetime.now(zone).date()
        this_monday = today - timedelta(days=today.weekday())
        start_d = this_monday - timedelta(days=7)
        days = 7
    end_d = date.fromisoformat(end) + timedelta(days=1) if end else start_d + timedelta(days=days)
    if end_d <= start_d:
        raise ValueError("end must be on or after start")
    return Window(
        start=datetime.combine(start_d, time.min, zone),
        end=datetime.combine(end_d, time.min, zone),
    )


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if " " in v and "T" not in v:
        v = v.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def people_in_window(
    people: Iterable[dict],
    window: Window,
    stop_after_older: int = 200,
) -> Iterator[dict]:
    """Filter a newest-first stream of people to those created in the window.

    Stops once `stop_after_older` consecutive records are older than the window
    start (a buffer because id order and created order can differ slightly,
    e.g. for imported leads)."""
    older_streak = 0
    for p in people:
        created = parse_ts(p.get("created"))
        if created is None:
            continue
        if created < window.start:
            older_streak += 1
            if older_streak >= stop_after_older:
                return
            continue
        older_streak = 0
        if created < window.end:
            yield p
