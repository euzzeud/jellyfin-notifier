import logging
import os

from waitress import serve

from jellyfin_notifier import create_app

logging.basicConfig(level=logging.INFO)

app = create_app()

if __name__ == "__main__":
    config = app.config["JF_CONFIG"]
    # In setup mode (no valid .env yet, or one still full of .env.example
    # placeholders - see create_app()) JF_CONFIG is None, since there's no
    # trustworthy config to build a Config object from - but the server
    # still needs a port to listen on so the setup wizard is reachable at
    # all. Falls back to the raw PORT env var (or .env.example's own
    # default, 5005) rather than crashing here.
    port = config.port if config is not None else int(os.environ.get("PORT", "5005"))
    # Serveur WSGI de production (waitress) au lieu du serveur de
    # développement Flask (app.run()) - celui-ci n'est pas fait pour tourner
    # en continu comme service (mono-thread par défaut, pas de vraie gestion
    # de charge, avertissement affiché à chaque démarrage). waitress reste
    # volontairement UN SEUL PROCESS avec un pool de threads (pas plusieurs
    # workers façon gunicorn) : le poller et le verrou settings.json ne
    # supportent qu'un seul process à la fois - plusieurs workers
    # dupliqueraient le thread du poller et enverraient chaque mail
    # plusieurs fois.
    serve(app, host="0.0.0.0", port=port, threads=6)
