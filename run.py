import logging

from jellyfin_notifier import create_app

logging.basicConfig(level=logging.INFO)

app = create_app()

if __name__ == "__main__":
    config = app.config["JF_CONFIG"]
    app.run(host="0.0.0.0", port=config.port)
