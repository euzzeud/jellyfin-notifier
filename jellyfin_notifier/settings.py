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

    # Couleurs du mail (éditables depuis l'onglet "Notifications ajout
    # d'items" avec des color pickers) - injectées dans email.html, qui garde
    # des styles inline (obligatoire pour la compat clients mail type
    # Outlook, pas de variables CSS possibles).
    color_bg: str = "#101010"
    color_card: str = "#18181b"
    color_header: str = "#101014"
    color_accent: str = "#AA5CC3"
    color_button: str = "#00A4DC"
    color_text: str = "#ffffff"
    color_muted: str = "#8a8a8e"

    # Surcharges optionnelles, éditables depuis la console API de l'admin,
    # sans avoir à modifier le .env ni redémarrer le service.
    jellyfin_api_key_override: str = ""
    jellyfin_url_override: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


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
