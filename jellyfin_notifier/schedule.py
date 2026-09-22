"""Fenêtre horaire/jours autorisée pour l'envoi des notifications."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .settings import Settings

WEEKDAY_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_within_window(settings: Settings, now: datetime | None = None) -> bool:
    """True si `now` tombe dans un jour ET une plage horaire autorisés."""
    now = now or datetime.now().astimezone()
    if now.weekday() not in settings.notify_days:
        return False

    start = _parse_hhmm(settings.notify_hour_start)
    end = _parse_hhmm(settings.notify_hour_end)
    current = now.time()

    if start <= end:
        return start <= current <= end
    # Fenêtre traversant minuit (ex: 22:00 -> 02:00).
    return current >= start or current <= end


def next_allowed_datetime(settings: Settings, from_dt: datetime | None = None) -> datetime:
    """Calcule le prochain instant où l'envoi sera autorisé (affiché dans le
    dashboard admin comme "prochain créneau"). Si on est déjà dans la
    fenêtre, retourne `from_dt` tel quel."""
    from_dt = from_dt or datetime.now().astimezone()
    if is_within_window(settings, from_dt):
        return from_dt

    start = _parse_hhmm(settings.notify_hour_start)
    for offset in range(0, 8):  # au pire, parcourt une semaine complète
        day = (from_dt + timedelta(days=offset)).date()
        slot = datetime.combine(day, start, tzinfo=from_dt.tzinfo)
        if slot <= from_dt:
            continue
        if slot.weekday() in settings.notify_days:
            return slot
    return from_dt  # fallback improbable (aucun jour coché)
