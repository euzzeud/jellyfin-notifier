"""Periodic polling of the Jellyfin API to detect new content, as a
replacement for the Webhook plugin (unreliable in practice in our setup).

Principle: every `poll_interval_seconds`, we ask Jellyfin for the N most
recently added items (sorted by creation date). Any ID not yet seen = new
content -> mail (or queued if outside the allowed time window, see
schedule.py). Seen IDs are persisted to disk to survive a service restart.

On the very first run (seen_items.json absent), the current state of the
library is recorded WITHOUT sending mail (otherwise it would spam the
whole existing catalog).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from . import metrics
from .atomic_json import atomic_write_text
from .config import Config
from .email_sender import item_from_api, send_email
from .jellyfin_client import JellyfinClient
from .pending import clear_pending, load_pending, save_pending
from .schedule import is_within_window, next_allowed_datetime
from .settings import Settings, load_settings

logger = logging.getLogger(__name__)

# No dedicated env var for this (unlike the interval or item types) - a
# named constant rather than a 200 hardcoded in both effective_limit() AND
# the admin dashboard placeholder, so the two can't silently diverge if
# one of them changes.
DEFAULT_POLLER_LIMIT = 200


class JellyfinPoller:
    def __init__(self, config: Config, client: JellyfinClient | None = None):
        self.config = config
        self._base_client = client or JellyfinClient(config.jellyfin_url, config.jellyfin_api_key, config.jellyfin_public_url)
        self.seen_path = Path(config.seen_items_path)
        self._bootstrap_needed = not self.seen_path.exists()
        self._seen_ids: set[str] = self._load_seen()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Guards every read-modify-write of self._seen_ids (+ the matching
        # file save) - without it, the background poll loop and a
        # manually-triggered "Poll now"/"Clear"/"Re-bootstrap" action (each
        # runs on its own thread) could race on the same in-memory set, not
        # just the file on disk.
        self._seen_lock = threading.Lock()
        # A non-blocking guard against two full poll cycles running at
        # once (the scheduled background cycle and a manual "Poll now" -
        # /poll-now calls poll_once() directly on the Flask request
        # thread). Overlapping cycles could double-count "new" items,
        # interleave their pending-queue/seen-items writes, or send the
        # same content twice - poll_once() below skips instead of running
        # a second cycle concurrently when this is already held.
        self._poll_lock = threading.Lock()

        # State exposed by /health and by the admin dashboard.
        self.last_poll_at: datetime | None = None
        self.last_fetch_success: bool | None = None
        self.last_fetch_error: str | None = None
        self.last_email_error: str | None = None

    def effective_item_types(self, settings: Settings) -> set[str]:
        raw = settings.poller_item_types_override.strip()
        if not raw:
            return self.config.notify_item_types
        return {t.strip() for t in raw.split(",") if t.strip()}

    def effective_interval(self, settings: Settings) -> int:
        return settings.poller_interval_seconds_override or self.config.poll_interval_seconds

    def effective_limit(self, settings: Settings) -> int:
        return settings.poller_limit_override or DEFAULT_POLLER_LIMIT

    @property
    def client(self) -> JellyfinClient:
        """Rebuilds the client if overrides (API key / URL) have been saved
        from the admin's API console - without this, changing the key in
        the admin would require restarting the service."""
        settings = load_settings(self.config.settings_path)
        url = settings.jellyfin_url_override or self.config.jellyfin_url
        key = settings.jellyfin_api_key_override or self.config.jellyfin_api_key
        if url == self._base_client.base_url and key == self._base_client.api_key:
            return self._base_client
        return JellyfinClient(url, key, self.config.jellyfin_public_url)

    def _load_seen(self) -> set[str]:
        if self.seen_path.exists():
            try:
                return set(json.loads(self.seen_path.read_text()))
            except Exception:
                logger.exception("Could not read %s, starting from an empty seen-items set", self.seen_path)
        return set()

    def _save_seen(self) -> None:
        """Callers must already hold self._seen_lock - this only does the
        file write, not the locking, since every call site also needs the
        lock held across its self._seen_ids mutation, not just this save."""
        atomic_write_text(self.seen_path, json.dumps(sorted(self._seen_ids)))

    def seen_count(self) -> int:
        return len(self._seen_ids)

    def seen_ids_sorted(self) -> list[str]:
        return sorted(self._seen_ids)

    def clear_seen(self) -> None:
        """Empties the "already seen" set entirely. The poller then treats
        every item currently in its fetch window as new on the next cycle -
        e.g. to deliberately test that new-content mail gets sent. For a
        routine reset (fixing a poller about to re-send old items) use
        rebootstrap_seen() instead, which doesn't risk a mail flood."""
        with self._seen_lock:
            self._seen_ids = set()
            self._save_seen()
        logger.warning("Already-seen items cleared entirely - the next poll may re-notify recently added content.")

    def rebootstrap_seen(self) -> tuple[bool, str]:
        """Re-fetches the current recent-items window from Jellyfin and
        marks it all as "already seen" WITHOUT sending any mail for it - the
        same safe step normally only run once, automatically, on a fresh
        install (see the bootstrap branch in _poll_once_impl). Returns
        (ok, error)."""
        settings = load_settings(self.config.settings_path)
        try:
            items = self.client.fetch_recent_items(
                self.effective_item_types(settings), limit=self.effective_limit(settings)
            )
        except Exception as exc:
            logger.exception("Failed to re-bootstrap already-seen items")
            return False, str(exc)
        with self._seen_lock:
            self._seen_ids = {it["Id"] for it in items if it.get("Id")}
            self._save_seen()
        logger.warning(
            "Already-seen items re-bootstrapped from the current library (%d item(s)), no mail sent.",
            len(items),
        )
        return True, ""

    def poll_once(self) -> list[dict]:
        """Runs one poll cycle (measured for /metrics: poll_*_duration_ms,
        poll_cycles_total, poll_errors_total) by delegating the actual work
        to _poll_once_impl(). Returns the new items ACTUALLY sent by mail
        (empty list if none, if this is the initial bootstrap, or if the
        detected items were queued outside the allowed window).

        Non-blocking on self._poll_lock: the scheduled background cycle and
        a manual "Poll now" (POST /poll-now, runs on the Flask request
        thread) both end up here, and running two cycles at once could
        double-count "new" items or interleave their pending/seen-items
        writes. Skipping the second call instead of blocking it also means
        a manual "Poll now" click gets an immediate, honest answer ("a
        cycle was already running") instead of silently waiting out however
        long the in-progress cycle takes."""
        if not self._poll_lock.acquire(blocking=False):
            logger.info("poll_once() skipped: a poll cycle is already running.")
            return []
        try:
            t0 = time.perf_counter()
            try:
                result = self._poll_once_impl()
            except Exception:
                metrics.record_poll(time.perf_counter() - t0, success=False)
                raise
            metrics.record_poll(time.perf_counter() - t0, success=bool(self.last_fetch_success))
            return result
        finally:
            self._poll_lock.release()

    def _poll_once_impl(self) -> list[dict]:
        self.last_poll_at = datetime.now().astimezone()
        settings = load_settings(self.config.settings_path)
        client = self.client

        if settings.poller_paused:
            logger.info("Poller paused (settings.poller_paused=true), cycle skipped")
            return []

        try:
            items = client.fetch_recent_items(
                self.effective_item_types(settings), limit=self.effective_limit(settings)
            )
            self.last_fetch_success = True
            self.last_fetch_error = None
        except Exception as exc:
            self.last_fetch_success = False
            self.last_fetch_error = str(exc)
            logger.exception("Error fetching recent items from Jellyfin")
            return []

        # Defensive filter: BoxSets (collections, e.g. "Prometheus - Saga")
        # sometimes come back via /Items even when filtered on
        # IncludeItemTypes=Movie,Series (auto-generated by the TMDb
        # scraper) - we never want to notify about these.
        items = [it for it in items if it.get("Type") != "BoxSet"]

        with self._seen_lock:
            new_items = [it for it in items if it.get("Id") not in self._seen_ids]

            # Only IDs currently in the "recently added" window are kept -
            # an item that has left this window can never trigger a mail
            # again, so there's no need to keep it in memory indefinitely
            # (a bounded file, never needing a manual "reset").
            self._seen_ids = {it["Id"] for it in items if it.get("Id")}
            self._save_seen()

        if self._bootstrap_needed:
            logger.info(
                "Initial bootstrap: %d existing item(s) recorded as already seen, no mail sent",
                len(items),
            )
            self._bootstrap_needed = False
            return []

        if not settings.new_notifications_enabled:
            # "New Content Notifications" module disabled from the admin:
            # items are already marked as seen above (no mail is ever sent
            # for them), but they aren't accumulated in the queue either -
            # otherwise re-enabling it would send everything that piled up
            # while it was off, all at once.
            if new_items:
                logger.info(
                    "%d new item(s) detected but 'New Content' notifications are disabled, no mail sent",
                    len(new_items),
                )
            return []

        parsed_new = [item_from_api(it) for it in new_items]
        pending = load_pending(self.config.pending_items_path)
        to_consider = pending + parsed_new

        if not to_consider:
            return []

        if not settings.smtp_enabled:
            # Mail sending disabled (the "Mail Server" page, pending
            # validation or turned off on purpose): items are kept in the
            # queue, as if outside the allowed window, rather than being
            # dropped - they'll go out as soon as sending is re-enabled.
            save_pending(self.config.pending_items_path, to_consider)
            logger.info(
                "%d item(s) to notify but mail sending is disabled (Mail Server page) -> queued (total pending: %d)",
                len(parsed_new),
                len(to_consider),
            )
            return []

        if not is_within_window(settings, self.last_poll_at):
            save_pending(self.config.pending_items_path, to_consider)
            logger.info(
                "%d item(s) detected outside the allowed window (%s-%s, days=%s) -> queued (total pending: %d)",
                len(parsed_new),
                settings.notify_hour_start,
                settings.notify_hour_end,
                settings.notify_days,
                len(to_consider),
            )
            return []

        logger.info("%d item(s) to notify (including %d already queued)", len(to_consider), len(pending))
        try:
            send_email(to_consider, self.config, client, settings, smtp=settings.resolve_smtp(self.config))
            clear_pending(self.config.pending_items_path)
            self.last_email_error = None
        except Exception as exc:
            self.last_email_error = str(exc)
            logger.exception("Failed to send mail from the poller")
            return []
        return to_consider

    def status(self) -> dict:
        """The poller's health state, exposed by /health (Grafana
        monitoring) and by the admin dashboard."""
        now = datetime.now().astimezone()
        seconds_since_last_poll = (
            (now - self.last_poll_at).total_seconds() if self.last_poll_at else None
        )
        # "stale" = no recent poll (2x the expected interval = a missed cycle).
        stale = (
            seconds_since_last_poll is None
            or seconds_since_last_poll > self.config.poll_interval_seconds * 2
        )
        healthy = bool(self.last_fetch_success) and not stale and self.last_email_error is None

        settings = load_settings(self.config.settings_path)
        pending_count = len(load_pending(self.config.pending_items_path))
        running = bool(self._thread and self._thread.is_alive())

        return {
            "poller_enabled": True,
            "running": running,
            "paused": settings.poller_paused,
            "notifications_enabled": settings.new_notifications_enabled,
            "mail_enabled": settings.smtp_enabled,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "seconds_since_last_poll": seconds_since_last_poll,
            "poll_interval_seconds": self.effective_interval(settings),
            "item_types": sorted(self.effective_item_types(settings)),
            "limit": self.effective_limit(settings),
            "last_fetch_success": self.last_fetch_success,
            "last_fetch_error": self.last_fetch_error,
            "last_email_error": self.last_email_error,
            "pending_count": pending_count,
            "within_window": is_within_window(settings, now),
            "next_allowed_at": next_allowed_datetime(settings, now).isoformat(),
            # Integer (1/0) rather than a JSON bool: Telegraf's "classic"
            # JSON parser silently ignores booleans (only numbers are
            # automatically converted into fields), so "healthy" as
            # true/false never made it into InfluxDB.
            "healthy": 1 if (healthy and running and not settings.poller_paused) else 0,
        }

    def _run(self) -> None:
        # Immediate first poll (bootstrap or catch-up), then loop at a
        # regular interval. The interval is re-read every cycle (can be
        # changed from the admin without restarting the thread).
        self.poll_once()
        while True:
            settings = load_settings(self.config.settings_path)
            if self._stop_event.wait(self.effective_interval(settings)):
                break
            self.poll_once()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jellyfin-poller")
        self._thread.start()
        logger.info(
            "Poller started (interval=%ss, types=%s)",
            self.config.poll_interval_seconds,
            self.config.notify_item_types,
        )

    def stop(self) -> None:
        self._stop_event.set()
