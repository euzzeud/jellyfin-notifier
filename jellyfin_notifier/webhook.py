"""Flask route that receives Jellyfin webhooks."""

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
    """A single buffer per process (key = the Config object's id)."""
    config = current_app.config["JF_CONFIG"]
    jf_client = JellyfinClient(config.jellyfin_url, config.jellyfin_api_key, config.jellyfin_public_url)

    key = id(config)
    if key not in _buffers_by_config_id:
        def flush(items: list[dict]):
            settings = load_settings(config.settings_path)
            if not settings.new_notifications_enabled:
                # "New Content Notifications" module disabled from the admin -
                # the webhook can stay active (e.g. if the poller is also
                # used in parallel) without ever sending mail.
                logger.info(
                    "%d item(s) received via webhook but 'New Content' notifications are disabled, no mail sent",
                    len(items),
                )
                return
            if not settings.smtp_enabled:
                # Mail sending disabled (the "Mail Server" page, pending
                # validation or turned off on purpose) - the webhook has no
                # queue like the poller does, so these items are lost (like
                # any item detected during an outage).
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
        return jsonify({"ignored": True, "reason": f"type {item_type} filtered out"}), 200

    item = item_from_payload(payload)
    _get_buffer().add(item)

    return jsonify({"queued": True}), 200


@webhook_bp.route("/health", methods=["GET"])
def health():
    """Service status, meant to be polled by a monitoring system (Grafana,
    etc). "healthy" reflects the poller's actual state (recent + successful
    last poll, last mail sent without error), not just "the Flask process
    responds". Deliberately exempt from the setup-mode redirect (see
    _SETUP_EXEMPT_ENDPOINTS in __init__.py) so it always returns a real
    answer - including a fresh install whose .env isn't filled in yet -
    instead of a 302 to /setup that a monitoring tool would read as down."""
    configured = current_app.config.get("JF_CONFIG") is not None
    poller = current_app.config.get("JF_POLLER")

    if not poller:
        # Either the poller is deliberately disabled (POLLER_ENABLED=false),
        # or there's no configuration yet (setup mode) - either way the
        # process is up and there's nothing more to report about the poller.
        return jsonify({"status": "ok", "configured": configured, "poller_enabled": False, "healthy": 1}), 200

    status = poller.status()
    http_code = 200 if status["healthy"] else 503
    return jsonify({"status": "ok", "configured": configured, **status}), http_code


@webhook_bp.route("/metrics", methods=["GET"])
def metrics_endpoint():
    """Detailed metrics for an external monitoring system (meant for
    Grafana + InfluxDB + Telegraf, see `inputs.http` with
    `data_format = "json"` to scrape this endpoint directly - no need for
    `inputs.exec` + curl). Unlike /health (just enough to know the service
    is up and "healthy", for a simple alert), this one also exposes
    counters cumulative since process startup (mails sent/failed by type,
    HTTP requests by status class, admin login attempts, poll cycle
    duration) on top of the poller's and queues' instantaneous state.
    Always FLAT (a single level, never a nested object) to stay compatible
    with Telegraf's "classic" JSON parser. Public like /health, for the
    same reasons (LAN scrape, no credentials to manage on the Telegraf
    side)."""
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
    """Triggers a polling cycle immediately (for testing without waiting
    for POLL_INTERVAL_SECONDS)."""
    poller = current_app.config.get("JF_POLLER")
    if not poller:
        return jsonify({"error": "poller disabled (POLLER_ENABLED=false)"}), 400

    new_items = poller.poll_once()
    return jsonify({"new_items_found": len(new_items), "names": [i["name"] for i in new_items]}), 200
