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
