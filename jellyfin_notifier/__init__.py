import hashlib

from flask import Flask

from .admin import admin_bp
from .config import Config
from .poller import JellyfinPoller
from .webhook import webhook_bp


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    cfg = config or Config.from_env()
    app.config["JF_CONFIG"] = cfg
    # Clé de session dérivée des identifiants admin : stable tant qu'ils ne
    # changent pas (pas besoin d'une variable d'env dédiée), et invalide
    # automatiquement les sessions ouvertes si le mot de passe est changé.
    app.secret_key = hashlib.sha256(f"{cfg.admin_username}:{cfg.admin_password}".encode()).hexdigest()
    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    if cfg.poller_enabled:
        poller = JellyfinPoller(cfg)
        poller.start()
        app.config["JF_POLLER"] = poller

    return app
