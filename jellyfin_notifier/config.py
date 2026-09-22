"""Chargement de la config depuis les variables d'environnement."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    gmail_address: str
    gmail_app_password: str
    sender_name: str
    recipients: list[str]

    jellyfin_url: str
    jellyfin_api_key: str
    jellyfin_public_url: str

    debounce_seconds: int
    notify_item_types: set[str]
    webhook_shared_secret: str
    port: int

    poll_interval_seconds: int
    seen_items_path: str
    poller_enabled: bool

    # Interface d'admin (/admin) : planning, éditeur de template, console API,
    # gestion du service, logs. Protégée par HTTP Basic Auth.
    admin_username: str
    admin_password: str
    settings_path: str
    pending_items_path: str
    upcoming_path: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            gmail_address=os.environ["GMAIL_ADDRESS"],
            gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
            sender_name=os.environ.get("SENDER_NAME", "Jellyfin"),
            recipients=[r.strip() for r in os.environ["NOTIFY_RECIPIENTS"].split(",") if r.strip()],
            jellyfin_url=os.environ.get("JELLYFIN_URL", "http://localhost:8096").rstrip("/"),
            jellyfin_api_key=os.environ.get("JELLYFIN_API_KEY", ""),
            jellyfin_public_url=os.environ.get("JELLYFIN_PUBLIC_URL", os.environ.get("JELLYFIN_URL", "http://localhost:8096")).rstrip("/"),
            debounce_seconds=int(os.environ.get("DEBOUNCE_SECONDS", "300")),
            notify_item_types={
                t.strip() for t in os.environ.get("NOTIFY_ITEM_TYPES", "Movie,Series").split(",") if t.strip()
            },
            webhook_shared_secret=os.environ.get("WEBHOOK_SHARED_SECRET", ""),
            port=int(os.environ.get("PORT", "5005")),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
            seen_items_path=os.environ.get("SEEN_ITEMS_PATH", "seen_items.json"),
            poller_enabled=os.environ.get("POLLER_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
            admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
            settings_path=os.environ.get("SETTINGS_PATH", "settings.json"),
            pending_items_path=os.environ.get("PENDING_ITEMS_PATH", "pending_items.json"),
            upcoming_path=os.environ.get("UPCOMING_PATH", "upcoming.json"),
        )
