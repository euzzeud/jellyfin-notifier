"""Chargement de la config depuis les variables d'environnement."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Encryption SMTP possibles : STARTTLS (587, le plus courant), SSL/TLS
# implicite (465), ou aucune (25, réseau interne uniquement).
SMTP_ENCRYPTIONS = ("starttls", "ssl", "none")


@dataclass
class Config:
    # Serveur SMTP - Gmail par défaut (compat historique), mais n'importe
    # quel fournisseur SMTP standard fonctionne (Outlook, OVH, un serveur
    # maison, etc).
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

    # Interface d'admin (racine du site) : planning, éditeur de template,
    # console API, gestion du service, logs. Protégée par une page de login.
    admin_username: str
    admin_password: str
    settings_path: str
    pending_items_path: str
    upcoming_path: str

    @classmethod
    def from_env(cls) -> "Config":
        # SMTP_USERNAME/SMTP_PASSWORD sont les variables "génériques" ;
        # GMAIL_ADDRESS/GMAIL_APP_PASSWORD restent acceptées en repli pour ne
        # pas casser les installations existantes (Gmail était le seul
        # fournisseur supporté au départ).
        smtp_username = os.environ.get("SMTP_USERNAME") or os.environ.get("GMAIL_ADDRESS")
        smtp_password = os.environ.get("SMTP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD")
        if not smtp_username:
            raise KeyError("SMTP_USERNAME (ou GMAIL_ADDRESS) manquant dans l'environnement")
        if not smtp_password:
            raise KeyError("SMTP_PASSWORD (ou GMAIL_APP_PASSWORD) manquant dans l'environnement")

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
        )


@dataclass
class SmtpSettings:
    """Paramètres SMTP effectifs pour un envoi donné - résultat de la fusion
    entre Config (.env, figé au démarrage) et les surcharges de Settings
    (éditables à chaud depuis l'admin). Voir Settings.resolve_smtp()."""
    host: str
    port: int
    encryption: str
    username: str
    password: str
    sender_name: str
    sender_email: str
    recipients: list[str]
