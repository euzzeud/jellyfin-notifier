"""Route Flask qui reçoit les webhooks Jellyfin."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from . import metrics
from .buffer import DebounceBuffer
from .email_sender import item_from_payload, send_email
from .jellyfin_client import JellyfinClient
from .pending import load_pending
from .settings import load_settings
from .upcoming import list_upcoming

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)

_buffers_by_config_id: dict[int, DebounceBuffer] = {}


def _get_buffer() -> DebounceBuffer:
    """Un seul buffer par process (clé = id de l'objet Config)."""
    config = current_app.config["JF_CONFIG"]
    jf_client = JellyfinClient(config.jellyfin_url, config.jellyfin_api_key, config.jellyfin_public_url)

    key = id(config)
    if key not in _buffers_by_config_id:
        def flush(items: list[dict]):
            settings = load_settings(config.settings_path)
            if not settings.new_notifications_enabled:
                # Module "New Content Notifications" désactivé depuis l'admin -
                # le webhook peut rester actif (ex: si le poller est aussi
                # utilisé en parallèle) sans jamais envoyer de mail.
                logger.info(
                    "%d item(s) received via webhook but 'New Content' notifications are disabled, no mail sent",
                    len(items),
                )
                return
            if not settings.smtp_enabled:
                # Envoi de mail désactivé (page "Mail Server", en attente de
                # validation ou coupé volontairement) - le webhook n'a pas de
                # file d'attente comme le poller, donc ces items sont perdus
                # (comme n'importe quel item détecté pendant une coupure).
                logger.info(
                    "%d item(s) received via webhook but mail sending is disabled (Mail Server page), no mail sent",
                    len(items),
                )
                return
            send_email(items, config, jf_client, settings, smtp=settings.resolve_smtp(config))

        _buffers_by_config_id[key] = DebounceBuffer(config.debounce_seconds, flush)
    return _buffers_by_config_id[key]


@webhook_bp.route("/jellyfin-webhook", methods=["POST"])
def jellyfin_webhook():
    config = current_app.config["JF_CONFIG"]

    if config.webhook_shared_secret:
        if request.headers.get("X-Webhook-Secret") != config.webhook_shared_secret:
            return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}

    if payload.get("NotificationType") != "ItemAdded":
        return jsonify({"ignored": True, "reason": "not ItemAdded"}), 200

    item_type = payload.get("ItemType")
    if item_type not in config.notify_item_types:
        return jsonify({"ignored": True, "reason": f"type {item_type} filtré"}), 200

    item = item_from_payload(payload)
    _get_buffer().add(item)

    return jsonify({"queued": True}), 200


@webhook_bp.route("/health", methods=["GET"])
def health():
    """Statut du service, pensé pour être interrogé par un système de
    supervision (Grafana, etc). "healthy" reflète l'état réel du poller
    (dernier poll récent + réussi + dernier mail envoyé sans erreur), pas
    juste "le process Flask répond"."""
    poller = current_app.config.get("JF_POLLER")

    if not poller:
        # Poller désactivé volontairement (POLLER_ENABLED=false) : le
        # process tourne, on ne peut rien dire de plus.
        return jsonify({"status": "ok", "poller_enabled": False, "healthy": 1}), 200

    status = poller.status()
    http_code = 200 if status["healthy"] else 503
    return jsonify({"status": "ok", **status}), http_code


@webhook_bp.route("/metrics", methods=["GET"])
def metrics_endpoint():
    """Métriques détaillées pour un système de supervision externe (pensé
    pour Grafana + InfluxDB + Telegraf, cf. `inputs.http` avec
    `data_format = "json"` pour scraper directement ce endpoint - pas besoin
    d'`inputs.exec` + curl). Contrairement à /health (juste de quoi savoir
    si le service est up et "healthy", pour une alerte simple), celui-ci
    expose aussi des compteurs cumulés depuis le démarrage du process
    (mails envoyés/échoués par type, requêtes HTTP par classe de statut,
    tentatives de connexion admin, durée des cycles de poll) en plus de
    l'état instantané du poller et des files d'attente. Toujours à PLAT (un
    seul niveau, jamais d'objet imbriqué) pour rester compatible avec le
    parseur JSON "classique" de Telegraf. Public comme /health, pour les
    mêmes raisons (scrape LAN, pas de credentials à gérer côté Telegraf)."""
    config = current_app.config["JF_CONFIG"]
    poller = current_app.config.get("JF_POLLER")

    data = metrics.snapshot()
    if poller:
        data.update(poller.status())
    else:
        data["pending_count"] = len(load_pending(config.pending_items_path))
    data["upcoming_count"] = len(list_upcoming(config.upcoming_path))
    return jsonify(data), 200


@webhook_bp.route("/poll-now", methods=["POST", "GET"])
def poll_now():
    """Déclenche un cycle de polling immédiatement (pour tester sans attendre
    POLL_INTERVAL_SECONDS)."""
    poller = current_app.config.get("JF_POLLER")
    if not poller:
        return jsonify({"error": "poller désactivé (POLLER_ENABLED=false)"}), 400

    new_items = poller.poll_once()
    return jsonify({"new_items_found": len(new_items), "names": [i["name"] for i in new_items]}), 200
