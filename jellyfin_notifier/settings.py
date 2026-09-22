"""Paramètres modifiables à chaud depuis l'interface d'admin, persistés dans
un fichier JSON (contrairement à Config qui vient de .env et est figé au
démarrage du process). Rechargés à chaque lecture -> les changements faits
dans l'admin s'appliquent sans redémarrer le service."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_TEMPLATE_SUBJECT_SINGLE = "Nouveauté sur Jellyfin : {name}"
DEFAULT_TEMPLATE_SUBJECT_MULTI = "Nouveautés sur Jellyfin : {count} ajouts"
DEFAULT_INTRO_SINGLE = "Un nouveau contenu est disponible !"
DEFAULT_INTRO_MULTI = "{count} nouveaux contenus sont disponibles !"
DEFAULT_FOOTER = "Envoyé automatiquement par ton serveur Jellyfin - Enzo GIOIELLI"

_lock = threading.Lock()


@dataclass
class Settings:
    # Jours autorisés pour l'envoi (0=Lundi ... 6=Dimanche, cf. datetime.weekday())
    notify_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    notify_hour_start: str = "08:00"
    notify_hour_end: str = "22:00"

    overview_max_length: int = 200

    template_subject_single: str = DEFAULT_TEMPLATE_SUBJECT_SINGLE
    template_subject_multi: str = DEFAULT_TEMPLATE_SUBJECT_MULTI
    template_intro_single: str = DEFAULT_INTRO_SINGLE
    template_intro_multi: str = DEFAULT_INTRO_MULTI
    template_footer: str = DEFAULT_FOOTER

    # Couleurs du mail (éditables depuis l'onglet "New Content Notifications"
    # avec des color pickers) - injectées dans email.html, qui garde des
    # styles inline (obligatoire pour la compat clients mail type Outlook,
    # pas de variables CSS possibles).
    color_bg: str = "#101010"
    color_card: str = "#18181b"
    color_header: str = "#101014"
    color_accent: str = "#AA5CC3"
    color_button: str = "#00A4DC"
    color_text: str = "#ffffff"
    color_muted: str = "#8a8a8e"

    # Même chose, mais pour le mail "Upcoming Content Notifications" (titres
    # annoncés manuellement, pas encore dans la bibliothèque) - textes et
    # couleurs entièrement séparés du mail "nouveau contenu", personnalisables
    # indépendamment depuis l'onglet Upcoming.
    upcoming_subject_single: str = "Bientôt disponible : {name}"
    upcoming_subject_multi: str = "Bientôt disponibles : {count} titres"
    upcoming_intro_single: str = "Un nouveau titre arrive bientôt !"
    upcoming_intro_multi: str = "{count} nouveaux titres arrivent bientôt !"
    upcoming_footer: str = DEFAULT_FOOTER
    upcoming_color_bg: str = "#101010"
    upcoming_color_card: str = "#18181b"
    upcoming_color_header: str = "#101014"
    upcoming_color_accent: str = "#AA5CC3"
    upcoming_color_button: str = "#00A4DC"
    upcoming_color_text: str = "#ffffff"
    upcoming_color_muted: str = "#8a8a8e"

    # Surcharges optionnelles, éditables depuis la console API de l'admin,
    # sans avoir à modifier le .env ni redémarrer le service.
    jellyfin_api_key_override: str = ""
    jellyfin_url_override: str = ""

    # Poller : éditable depuis le dashboard de l'admin, sans redémarrer le
    # service. Vide/0 = valeur de .env (Config) utilisée telle quelle.
    poller_item_types_override: str = ""  # ex: "Movie,Series" - vide = Config.notify_item_types
    poller_interval_seconds_override: int = 0  # 0 = Config.poll_interval_seconds
    poller_limit_override: int = 0  # 0 = valeur par défaut (200)
    poller_paused: bool = False  # true = le thread tourne mais ne fait rien à chaque cycle

    # Serveur SMTP / expéditeur / destinataires - éditables depuis l'admin,
    # sans redémarrer le service. Vide/0 = valeur de .env (Config) utilisée
    # telle quelle. N'importe quel fournisseur SMTP standard est supporté,
    # pas seulement Gmail.
    smtp_host_override: str = ""
    smtp_port_override: int = 0
    smtp_encryption_override: str = ""  # "" = Config, sinon "starttls"/"ssl"/"none"
    smtp_username_override: str = ""
    smtp_password_override: str = ""
    sender_name_override: str = ""
    sender_email_override: str = ""
    recipients_override: str = ""  # séparés par des virgules

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def scoped(self, scope: str) -> "ScopedEmailSettings":
        """Vue avec les noms d'attributs génériques attendus par
        email_sender.py (template_subject_single, color_bg, ...), pointant
        soit sur les champs "nouveau contenu", soit sur les champs
        "upcoming" - permet de réutiliser tout email_sender.py tel quel pour
        les deux contextes de mail, chacun personnalisable séparément."""
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
        """Fusionne les valeurs de .env (Config, figées au démarrage) avec
        les surcharges éditables depuis l'admin (page "Mail server") -
        n'importe quel champ vide/0 dans Settings retombe sur la valeur de
        Config."""
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
    """Mêmes attributs que Settings pour la partie "texte/couleurs du mail" -
    ce que email_sender.py consomme, indépendamment du scope (new/upcoming)."""
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
