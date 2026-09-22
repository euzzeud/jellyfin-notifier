"""Settings that can be changed on the fly from the admin interface,
persisted in a JSON file (unlike Config, which comes from .env and is
fixed at process startup). Reloaded on every read -> changes made in the
admin apply without restarting the service."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Only needed for the resolve_smtp() return type below - imported lazily
    # at runtime instead (inside the function) to avoid a circular import
    # with config.py.
    from .config import SmtpSettings

DEFAULT_TEMPLATE_SUBJECT_SINGLE = "New on Jellyfin: {name}"
DEFAULT_TEMPLATE_SUBJECT_MULTI = "New on Jellyfin: {count} additions"
DEFAULT_INTRO_SINGLE = "New content is available!"
DEFAULT_INTRO_MULTI = "{count} new items are available!"
DEFAULT_FOOTER = "Automatically sent by Jellyfin - Enzo GIOIELLI"

_lock = threading.Lock()


@dataclass
class Settings:
    # Allowed days for sending (0=Monday ... 6=Sunday, see datetime.weekday())
    notify_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    notify_hour_start: str = "08:00"
    notify_hour_end: str = "22:00"

    overview_max_length: int = 200

    template_subject_single: str = DEFAULT_TEMPLATE_SUBJECT_SINGLE
    template_subject_multi: str = DEFAULT_TEMPLATE_SUBJECT_MULTI
    template_intro_single: str = DEFAULT_INTRO_SINGLE
    template_intro_multi: str = DEFAULT_INTRO_MULTI
    template_footer: str = DEFAULT_FOOTER

    # Mail colors (editable from the "New Content Notifications" tab with
    # color pickers) - injected into email.html, which keeps inline styles
    # (required for compatibility with mail clients like Outlook, no CSS
    # variables possible).
    color_bg: str = "#101010"
    color_card: str = "#18181b"
    color_header: str = "#101014"
    color_accent: str = "#AA5CC3"
    color_button: str = "#00A4DC"
    color_text: str = "#ffffff"
    color_muted: str = "#8a8a8e"

    # Same thing, but for the "Upcoming Content Notifications" mail (titles
    # announced manually, not yet in the library) - text and colors
    # entirely separate from the "new content" mail, customizable
    # independently from the Upcoming tab.
    upcoming_subject_single: str = "Coming soon: {name}"
    upcoming_subject_multi: str = "Coming soon: {count} titles"
    upcoming_intro_single: str = "A new title is coming soon!"
    upcoming_intro_multi: str = "{count} new titles are coming soon!"
    upcoming_footer: str = DEFAULT_FOOTER
    upcoming_color_bg: str = "#101010"
    upcoming_color_card: str = "#18181b"
    upcoming_color_header: str = "#101014"
    upcoming_color_accent: str = "#AA5CC3"
    upcoming_color_button: str = "#00A4DC"
    upcoming_color_text: str = "#ffffff"
    upcoming_color_muted: str = "#8a8a8e"

    # Optional overrides, editable from the admin's API console, without
    # having to modify .env or restart the service.
    jellyfin_api_key_override: str = ""
    jellyfin_url_override: str = ""

    # Poller: editable from the admin dashboard, without restarting the
    # service. Empty/0 = the .env (Config) value used as-is.
    poller_item_types_override: str = ""  # e.g. "Movie,Series" - empty = Config.notify_item_types
    poller_interval_seconds_override: int = 0  # 0 = Config.poll_interval_seconds
    poller_limit_override: int = 0  # 0 = default value (200)
    poller_paused: bool = False  # true = the thread keeps running but does nothing each cycle

    # SMTP server / sender / recipients - editable from the admin, without
    # restarting the service. Empty/0 = the .env (Config) value used
    # as-is. Any standard SMTP provider is supported, not just Gmail.
    smtp_host_override: str = ""
    smtp_port_override: int = 0
    smtp_encryption_override: str = ""  # "" = Config, sinon "starttls"/"ssl"/"none"
    smtp_username_override: str = ""
    smtp_password_override: str = ""
    sender_name_override: str = ""
    sender_email_override: str = ""
    recipients_override: str = ""  # comma-separated

    # General mail-sending switch - disabled by default until someone has
    # validated the SMTP configuration with a real connection test (the
    # "Mail Server" page). smtp_validated_fingerprint is the fingerprint
    # of the config last tested successfully; if it no longer matches the
    # current effective config (a field has been changed since), the admin
    # automatically flips the switch back to false on save, so unverified
    # credentials are never left silently enabled.
    smtp_enabled: bool = False
    smtp_validated_fingerprint: str = ""

    # Visual "stackable blocks" (drag-and-drop) editor: block list
    # serialized as JSON, kept separate for each mail (new / upcoming) like
    # the rest of the text customization. Compiled on save into HTML/Jinja2
    # via email_blocks.compile_blocks_to_html(), which then overwrites the
    # raw template (email.html / email_upcoming.html) - this field only
    # exists so the visual editor can be reopened with the same blocks.
    email_blocks: str = "[]"
    upcoming_email_blocks: str = "[]"

    # Independent switches for each notification module - lets one be
    # turned off without touching the other (e.g. disabling "Upcoming"
    # without stopping the "New Content" poller). When "new" is disabled,
    # the poller keeps running and marks items as seen WITHOUT sending mail
    # (to avoid triggering a flood of catch-up notifications when it's
    # re-enabled); when "upcoming" is disabled, the announcement-send
    # button is blocked server-side.
    new_notifications_enabled: bool = True
    upcoming_notifications_enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def scoped(self, scope: str) -> "ScopedEmailSettings":
        """A view with the generic attribute names expected by
        email_sender.py (template_subject_single, color_bg, ...), pointing
        either at the "new content" fields or the "upcoming" fields - lets
        all of email_sender.py be reused as-is for both mail contexts,
        each customizable separately."""
        if scope == "upcoming":
            return ScopedEmailSettings(
                template_subject_single=self.upcoming_subject_single,
                template_subject_multi=self.upcoming_subject_multi,
                template_intro_single=self.upcoming_intro_single,
                template_intro_multi=self.upcoming_intro_multi,
                template_footer=self.upcoming_footer,
                overview_max_length=self.overview_max_length,
                color_bg=self.upcoming_color_bg,
                color_card=self.upcoming_color_card,
                color_header=self.upcoming_color_header,
                color_accent=self.upcoming_color_accent,
                color_button=self.upcoming_color_button,
                color_text=self.upcoming_color_text,
                color_muted=self.upcoming_color_muted,
            )
        return ScopedEmailSettings(
            template_subject_single=self.template_subject_single,
            template_subject_multi=self.template_subject_multi,
            template_intro_single=self.template_intro_single,
            template_intro_multi=self.template_intro_multi,
            template_footer=self.template_footer,
            overview_max_length=self.overview_max_length,
            color_bg=self.color_bg,
            color_card=self.color_card,
            color_header=self.color_header,
            color_accent=self.color_accent,
            color_button=self.color_button,
            color_text=self.color_text,
            color_muted=self.color_muted,
        )

    def resolve_smtp(self, config) -> "SmtpSettings":
        """Merges .env values (Config, fixed at startup) with the overrides
        editable from the admin (the "Mail Server" page) - any empty/0
        field in Settings falls back to Config's value."""
        from .config import SmtpSettings

        encryption = self.smtp_encryption_override.strip() or config.smtp_encryption
        if self.recipients_override.strip():
            recipients = [r.strip() for r in self.recipients_override.split(",") if r.strip()]
        else:
            recipients = config.recipients

        return SmtpSettings(
            host=self.smtp_host_override.strip() or config.smtp_host,
            port=self.smtp_port_override or config.smtp_port,
            encryption=encryption,
            username=self.smtp_username_override.strip() or config.smtp_username,
            password=self.smtp_password_override or config.smtp_password,
            sender_name=self.sender_name_override.strip() or config.sender_name,
            sender_email=self.sender_email_override.strip() or config.sender_email,
            recipients=recipients,
        )


@dataclass
class ScopedEmailSettings:
    """Same attributes as Settings for the "mail text/colors" part - what
    email_sender.py consumes, regardless of scope (new/upcoming)."""
    template_subject_single: str
    template_subject_multi: str
    template_intro_single: str
    template_intro_multi: str
    template_footer: str
    overview_max_length: int
    color_bg: str
    color_card: str
    color_header: str
    color_accent: str
    color_button: str
    color_text: str
    color_muted: str


def load_settings(path: str) -> Settings:
    p = Path(path)
    if not p.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(p.read_text()))
    except Exception:
        return Settings()


def save_settings(path: str, settings: Settings) -> None:
    p = Path(path)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False))
