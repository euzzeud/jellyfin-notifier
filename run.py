import logging

from waitress import serve

from jellyfin_notifier import create_app

logging.basicConfig(level=logging.INFO)

app = create_app()

if __name__ == "__main__":
    config = app.config["JF_CONFIG"]
    # Serveur WSGI de production (waitress) au lieu du serveur de
    # développement Flask (app.run()) - celui-ci n'est pas fait pour tourner
    # en continu comme service (mono-thread par défaut, pas de vraie gestion
    # de charge, avertissement affiché à chaque démarrage). waitress reste
    # volontairement UN SEUL PROCESS avec un pool de threads (pas plusieurs
    # workers façon gunicorn) : le poller et le verrou settings.json ne
    # supportent qu'un seul process à la fois - plusieurs workers
    # dupliqueraient le thread du poller et enverraient chaque mail
    # plusieurs fois.
    serve(app, host="0.0.0.0", port=config.port, threads=6)
