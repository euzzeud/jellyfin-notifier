"""Polling périodique de l'API Jellyfin pour détecter les nouveaux contenus,
en remplacement du plugin Webhook (peu fiable en pratique dans notre setup).

Principe : toutes les `poll_interval_seconds`, on demande à Jellyfin les N
derniers items ajoutés (triés par date de création). Tout ID pas encore vu
= nouveau contenu -> mail (ou mise en file d'attente si on est hors du
créneau horaire autorisé, cf. schedule.py). Les IDs vus sont persistés sur
disque pour survivre à un redémarrage du service.

Au tout premier lancement (fichier seen_items.json absent), on enregistre
l'état actuel de la bibliothèque SANS envoyer de mail (sinon on spamme avec
tout le catalogue existant).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .email_sender import item_from_api, send_email
from .jellyfin_client import JellyfinClient
from .pending import clear_pending, load_pending, save_pending
from .schedule import is_within_window, next_allowed_datetime
from .settings import Settings, load_settings

logger = logging.getLogger(__name__)


class JellyfinPoller:
    def __init__(self, config: Config, client: JellyfinClient | None = None):
        self.config = config
        self._base_client = client or JellyfinClient(config.jellyfin_url, config.jellyfin_api_key, config.jellyfin_public_url)
        self.seen_path = Path(config.seen_items_path)
        self._bootstrap_needed = not self.seen_path.exists()
        self._seen_ids: set[str] = self._load_seen()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # État exposé par /health et par le dashboard admin.
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
        return settings.poller_limit_override or 200

    @property
    def client(self) -> JellyfinClient:
        """Reconstruit le client si des surcharges (clé API / URL) ont été
        enregistrées depuis la console API de l'admin - sans ça, changer la
        clé dans l'admin nécessiterait de redémarrer le service."""
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
                logger.exception("Impossible de lire %s, on repart de zéro", self.seen_path)
        return set()

    def _save_seen(self) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(sorted(self._seen_ids)))

    def poll_once(self) -> list[dict]:
        """Fait un cycle de poll. Retourne les nouveaux items EFFECTIVEMENT
        envoyés par mail (liste vide si aucun, si c'est le bootstrap initial,
        ou si les items détectés ont été mis en file d'attente hors créneau)."""
        self.last_poll_at = datetime.now().astimezone()
        settings = load_settings(self.config.settings_path)
        client = self.client

        if settings.poller_paused:
            logger.info("Poller en pause (settings.poller_paused=true), cycle ignoré")
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
            logger.exception("Erreur en récupérant les items récents depuis Jellyfin")
            return []

        # Filtre défensif : les BoxSet (collections, ex: "Prometheus - Saga")
        # remontent parfois via /Items même filtré sur IncludeItemTypes=Movie,Series
        # (auto-générées par le scraper TMDb) - on ne veut jamais les notifier.
        items = [it for it in items if it.get("Type") != "BoxSet"]

        new_items = [it for it in items if it.get("Id") not in self._seen_ids]

        # On ne garde que les IDs actuellement dans la fenêtre "derniers ajouts" -
        # un item sorti de cette fenêtre ne peut plus jamais redéclencher de mail,
        # donc pas besoin de le garder en mémoire indéfiniment (fichier borné,
        # jamais besoin de le "reset" à la main).
        self._seen_ids = {it["Id"] for it in items if it.get("Id")}
        self._save_seen()

        if self._bootstrap_needed:
            logger.info(
                "Bootstrap initial : %d item(s) existant(s) enregistrés comme déjà vus, pas de mail envoyé",
                len(items),
            )
            self._bootstrap_needed = False
            return []

        if not settings.new_notifications_enabled:
            # Module "New Content Notifications" désactivé depuis l'admin :
            # les items sont déjà marqués comme vus ci-dessus (pas de mail
            # jamais envoyé pour eux), mais on ne les accumule pas non plus
            # en file d'attente - sinon la réactivation enverrait d'un coup
            # tout ce qui s'est accumulé pendant que c'était coupé.
            if new_items:
                logger.info(
                    "%d nouvel(nouveaux) item(s) détecté(s) mais notifications 'New Content' désactivées, aucun mail envoyé",
                    len(new_items),
                )
            return []

        parsed_new = [item_from_api(it) for it in new_items]
        pending = load_pending(self.config.pending_items_path)
        to_consider = pending + parsed_new

        if not to_consider:
            return []

        if not is_within_window(settings, self.last_poll_at):
            save_pending(self.config.pending_items_path, to_consider)
            logger.info(
                "%d item(s) détecté(s) hors créneau autorisé (%s-%s, jours=%s) -> mis en file d'attente (total en attente: %d)",
                len(parsed_new),
                settings.notify_hour_start,
                settings.notify_hour_end,
                settings.notify_days,
                len(to_consider),
            )
            return []

        logger.info("%d item(s) à notifier (dont %d en file d'attente)", len(to_consider), len(pending))
        try:
            send_email(to_consider, self.config, client, settings, smtp=settings.resolve_smtp(self.config))
            clear_pending(self.config.pending_items_path)
            self.last_email_error = None
        except Exception as exc:
            self.last_email_error = str(exc)
            logger.exception("Échec d'envoi du mail depuis le poller")
            return []
        return to_consider

    def status(self) -> dict:
        """État de santé du poller, exposé par /health (supervision Grafana)
        et par le dashboard admin."""
        now = datetime.now().astimezone()
        seconds_since_last_poll = (
            (now - self.last_poll_at).total_seconds() if self.last_poll_at else None
        )
        # "stale" = pas de poll récent (2x l'intervalle attendu = un cycle raté).
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
            # Entier (1/0) plutôt que bool JSON : le parser JSON "classique" de
            # Telegraf ignore silencieusement les booléens (seuls les nombres
            # sont convertis en champs automatiquement), donc "healthy" comme
            # true/false n'atteignait jamais InfluxDB.
            "healthy": 1 if (healthy and running and not settings.poller_paused) else 0,
        }

    def _run(self) -> None:
        # Premier poll immédiat (bootstrap ou rattrapage), puis boucle à intervalle régulier.
        # L'intervalle est relu à chaque cycle (peut être changé depuis l'admin
        # sans redémarrer le thread).
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
            "Poller démarré (intervalle=%ss, types=%s)",
            self.config.poll_interval_seconds,
            self.config.notify_item_types,
        )

    def stop(self) -> None:
        self._stop_event.set()
