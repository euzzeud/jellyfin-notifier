import hashlib
import logging
from datetime import datetime, timezone

from flask import Flask, request

from . import metrics
from .admin import admin_bp
from .config import Config
from .poller import JellyfinPoller
from .webhook import webhook_bp

# Logger dédié aux requêtes HTTP de l'interface d'admin (page Logs -> filtre
# "Interface") - waitress (le serveur de prod, cf. run.py) ne logue PAS les
# requêtes comme le faisait le serveur de dev Flask/werkzeug, donc sans ça
# la page Logs n'aurait plus aucune trace de qui a fait quoi dans l'admin.
_interface_logger = logging.getLogger("jellyfin_notifier.interface")

# Endpoints volontairement pas logués ici : très fréquents et sans intérêt
# de supervision (sinon la page Logs se remplit d'elle-même en boucle dès
# qu'on la laisse ouverte avec l'auto-refresh, ou qu'on tape dans un champ
# avec aperçu live) - les actions qui comptent (save/toggle/reset/envoi...)
# sont, elles, loguées explicitement dans admin.py.
_SKIP_ACCESS_LOG_PATHS = {"/logs/data"}
_SKIP_ACCESS_LOG_PREFIXES = ("/assets/",)
_SKIP_ACCESS_LOG_SUFFIXES = ("/live-preview",)


def _format_timestamp(value: str | None) -> str:
    """Jinja filter: turns a raw ISO 8601 timestamp (as stored/returned by
    poller.status() - e.g. "2026-09-22T20:10:21.727673+00:00", kept as-is
    there for /health and /metrics consumers) into a friendly display form
    for the admin UI, e.g. "22 Sep 2026, 20:10:21 UTC". Falls back to the
    raw value if it can't be parsed."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    tz_label = "UTC" if dt.tzinfo == timezone.utc else (dt.strftime("%Z") or "")
    formatted = dt.strftime("%d %b %Y, %H:%M:%S")
    return f"{formatted} {tz_label}".strip()


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    cfg = config or Config.from_env()
    app.config["JF_CONFIG"] = cfg
    app.jinja_env.filters["friendly_dt"] = _format_timestamp

    @app.after_request
    def _log_interface_access(response):
        metrics.inc_http(response.status_code)
        path = request.path
        if (
            path in _SKIP_ACCESS_LOG_PATHS
            or path.startswith(_SKIP_ACCESS_LOG_PREFIXES)
            or path.endswith(_SKIP_ACCESS_LOG_SUFFIXES)
        ):
            return response
        level = logging.INFO
        if response.status_code >= 500:
            level = logging.ERROR
        elif response.status_code >= 400:
            level = logging.WARNING
        _interface_logger.log(level, "%s %s -> %s (%s)", request.method, path, response.status_code, request.remote_addr)
        return response
    # Sans ça, Jinja met les templates compilés en cache indéfiniment tant
    # que le process tourne (app.debug=False ici, donc auto_reload serait
    # False par défaut) : modifier un fichier .html sur le LXC sans
    # redémarrer le service ne se verrait jamais. Coût négligeable (un
    # check de mtime par rendu) pour un outil d'admin LAN peu sollicité -
    # ça évite surtout de perdre du temps à déboguer une "vieille" page.
    # Les changements de code Python (.py), eux, nécessitent toujours un
    # redémarrage du service : ça, Python ne le recharge jamais à chaud.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # Clé de session dérivée des identifiants admin : stable tant qu'ils ne
    # changent pas (pas besoin d'une variable d'env dédiée), et invalide
    # automatiquement les sessions ouvertes si le mot de passe est changé.
    app.secret_key = hashlib.sha256(f"{cfg.admin_username}:{cfg.admin_password}".encode()).hexdigest()
    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)

    # Le poller est toujours instancié (permet de le démarrer/arrêter depuis
    # l'admin même si POLLER_ENABLED=false au démarrage), mais ne tourne au
    # lancement du service que si POLLER_ENABLED=true.
    poller = JellyfinPoller(cfg)
    if cfg.poller_enabled:
        poller.start()
    app.config["JF_POLLER"] = poller

    return app
