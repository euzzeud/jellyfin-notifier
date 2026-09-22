"""Allowed day/time window for sending notifications."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .settings import Settings

WEEKDAY_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_within_window(settings: Settings, now: datetime | None = None) -> bool:
    """True if `now` falls on an allowed day AND within the allowed time range."""
    now = now or datetime.now().astimezone()
    if now.weekday() not in settings.notify_days:
        return False

    start = _parse_hhmm(settings.notify_hour_start)
    end = _parse_hhmm(settings.notify_hour_end)
    current = now.time()

    if start <= end:
        return start <= current <= end
    # Window crossing midnight (e.g. 22:00 -> 02:00).
    return current >= start or current <= end


def next_allowed_datetime(settings: Settings, from_dt: datetime | None = None) -> datetime:
    """Computes the next moment sending will be allowed (shown in the admin
    dashboard as "next window"). If already within the window, returns
    `from_dt` as-is."""
    from_dt = from_dt or datetime.now().astimezone()
    if is_within_window(settings, from_dt):
        return from_dt

    start = _parse_hhmm(settings.notify_hour_start)
    for offset in range(0, 8):  # worst case, scans a full week
        day = (from_dt + timedelta(days=offset)).date()
        slot = datetime.combine(day, start, tzinfo=from_dt.tzinfo)
        if slot <= from_dt:
            continue
        if slot.weekday() in settings.notify_days:
            return slot
    return from_dt  # unlikely fallback (no day checked at all)
