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
    # Production WSGI server (waitress) instead of Flask's development
    # server (app.run()) - the latter isn't meant to run continuously as
    # a service (single-threaded by default, no real load handling, a
    # warning shown on every startup). waitress deliberately stays a
    # SINGLE PROCESS with a thread pool (not multiple gunicorn-style
    # workers): the poller and the settings.json lock only support a
    # single process at a time - multiple workers would duplicate the
    # poller's thread and send each mail multiple times.
    serve(app, host="0.0.0.0", port=port, threads=6)
