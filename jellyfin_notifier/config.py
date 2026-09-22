"""Loads the configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Possible SMTP encryption modes: STARTTLS (587, the most common), implicit
# SSL/TLS (465), or none (25, internal network only).
SMTP_ENCRYPTIONS = ("starttls", "ssl", "none")


@dataclass
class Config:
    # SMTP server - Gmail by default (historical compatibility), but any
    # standard SMTP provider works (Outlook, OVH, a self-hosted server,
    # etc).
    smtp_host: str
    smtp_port: int
    smtp_encryption: str  # "starttls" | "ssl" | "none"
    smtp_username: str
    smtp_password: str
    sender_email: str
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

    # Admin interface (site root): schedule, template editor, API console,
    # service management, logs. Protected by a login page.
    admin_username: str
    admin_password: str
    settings_path: str
    pending_items_path: str
    upcoming_path: str
    mail_history_path: str

    @classmethod
    def from_env(cls) -> "Config":
        # SMTP_USERNAME/SMTP_PASSWORD are the "generic" variables;
        # GMAIL_ADDRESS/GMAIL_APP_PASSWORD are still accepted as a fallback
        # so existing installs don't break (Gmail was the only supported
        # provider at first).
        smtp_username = os.environ.get("SMTP_USERNAME") or os.environ.get("GMAIL_ADDRESS")
        smtp_password = os.environ.get("SMTP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD")
        if not smtp_username:
            raise KeyError("SMTP_USERNAME (or GMAIL_ADDRESS) missing from the environment")
        if not smtp_password:
            raise KeyError("SMTP_PASSWORD (or GMAIL_APP_PASSWORD) missing from the environment")

        encryption = os.environ.get("SMTP_ENCRYPTION", "starttls").strip().lower()
        if encryption not in SMTP_ENCRYPTIONS:
            encryption = "starttls"

        return cls(
            smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_encryption=encryption,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            sender_email=os.environ.get("SENDER_EMAIL", smtp_username),
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
            mail_history_path=os.environ.get("MAIL_HISTORY_PATH", "mail_history.json"),
        )


@dataclass
class SmtpSettings:
    """Effective SMTP parameters for a given send - the result of merging
    Config (.env, fixed at startup) with Settings' overrides (editable live
    from the admin). See Settings.resolve_smtp()."""
    host: str
    port: int
    encryption: str
    username: str
    password: str
    sender_name: str
    sender_email: str
    recipients: list[str]
