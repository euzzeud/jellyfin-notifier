from flask import Flask

from .admin import admin_bp
from .config import Config
from .poller import JellyfinPoller
from .webhook import webhook_bp


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    cfg = config or Config.from_env()
    app.config["JF_CONFIG"] = cfg
    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    if cfg.poller_enabled:
        poller = JellyfinPoller(cfg)
        poller.start()
        app.config["JF_POLLER"] = poller

    return app
