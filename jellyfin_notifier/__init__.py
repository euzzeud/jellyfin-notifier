import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, request, url_for

from . import env_setup, metrics
from .admin import admin_bp
from .config import Config
from .poller import JellyfinPoller
from .setup_wizard import setup_bp
from .webhook import webhook_bp

# Endpoints reachable even while no valid configuration exists yet (first
# run, or a botched .env edit) - everything else redirects to the setup
# wizard until it's resolved. See setup_wizard.py. /health is exempted too
# (rather than redirecting to /setup) so deploy.sh's health check and any
# external monitoring keep getting a real answer during setup mode instead
# of a 302 they'd mistake for the service being down.
_SETUP_EXEMPT_ENDPOINTS = {
    "setup.setup_view", "setup.setup_save", "setup.setup_import", "setup.setup_test_jellyfin",
    "admin.assets", "webhook.health",
    # Flask's built-in static file server (css/*.css, favicon) - without
    # this, every stylesheet request during setup mode (exactly when the
    # setup wizard itself, the one page guaranteed reachable, needs its
    # CSS) gets redirected back to /setup instead of returning the file,
    # leaving the wizard completely unstyled on a fresh install.
    "static",
}

# Logger dedicated to the admin interface's HTTP requests (Logs page ->
# "Interface" filter) - waitress (the production server, see run.py) does
# NOT log requests the way the Flask/werkzeug dev server did, so without
# this the Logs page would have no trace at all of who did what in the admin.
_interface_logger = logging.getLogger("jellyfin_notifier.interface")

# Endpoints deliberately not logged here: very frequent and of no
# supervision interest (otherwise the Logs page fills itself up in a loop as
# soon as it's left open with auto-refresh, or while typing in a field with
# a live preview) - the actions that matter (save/toggle/reset/send...) are
# logged explicitly in admin.py instead.
_SKIP_ACCESS_LOG_PATHS = {"/logs/data"}
_SKIP_ACCESS_LOG_PREFIXES = ("/assets/",)
_SKIP_ACCESS_LOG_SUFFIXES = ("/live-preview",)


def _format_timestamp(value: str | None) -> str:
    """Jinja filter: turns a raw ISO 8601 timestamp (as stored/returned by
    poller.status() - e.g. "2026-09-22T20:10:21.727673+00:00", kept as-is
    there for /health and /metrics consumers) into a friendly display form
    for the admin UI, e.g. "22 Sep 2026, 22:10:21". Always converted to the
    local timezone of whoever/wherever is running this (the user's machine
    in dev, the LXC's own local time in production) and shown WITHOUT a
    "UTC"/"UTC+02:00"-style suffix - the admin is reading their own clock,
    a timezone label is just noise. Falls back to the raw value if it
    can't be parsed."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%d %b %Y, %H:%M:%S")


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    env_path = Path(os.environ.get("ENV_FILE_PATH", ".env"))
    setup_error = None
    cfg = config
    if cfg is None:
        # .env is normally injected by systemd's EnvironmentFile= before the
        # process even starts, but the setup wizard's "restart" (an in-place
        # os.execv, not a systemd restart) needs the freshly written file to
        # be picked up too - load_into_environ() reads it directly from disk
        # as a fallback, without overriding whatever systemd already set.
        env_setup.load_into_environ(env_path)
        try:
            cfg = Config.from_env()
        except Exception as exc:
            # No valid configuration yet (first run on a fresh install, or a
            # .env edited by hand into a broken state) - the app still boots,
            # but every page redirects to the setup wizard until this is
            # resolved (see _require_setup below and setup_wizard.py).
            setup_error = str(exc)
            logging.getLogger(__name__).warning("Starting in setup mode: %s", setup_error)
        else:
            # Config.from_env() only checks that required values are present,
            # not that they're real: a .env fresh-copied from .env.example
            # (e.g. by deploy.sh, or `cp .env.example .env` by hand) has
            # every required key "filled in" with an example value like
            # SMTP_PASSWORD=xxxxxxxxxxxxxxxx or ADMIN_PASSWORD=change-me,
            # which would otherwise boot straight into a broken, insecure
            # "configured" install without ever showing the setup wizard.
            unresolved = env_setup.unresolved_keys(os.environ)
            if unresolved:
                setup_error = (
                    "Still using the .env.example placeholder value for: "
                    + ", ".join(unresolved)
                )
                cfg = None
                logging.getLogger(__name__).warning("Starting in setup mode: %s", setup_error)

    app.config["JF_CONFIG"] = cfg
    app.config["JF_SETUP_ERROR"] = setup_error
    app.config["JF_ENV_PATH"] = env_path
    app.jinja_env.filters["friendly_dt"] = _format_timestamp

    # SESSION_COOKIE_SAMESITE: Flask doesn't default this to anything (it's
    # unset unless explicitly configured), so the session cookie would be
    # sent on cross-site requests too - e.g. a form on another site that
    # POSTs to this app, or a plain cross-site GET, would ride along with
    # the admin's session. "Lax" (the safer default most other frameworks
    # ship with) still allows normal top-level navigation (following a link
    # to the app) but blocks the cookie on cross-site POSTs/subresource
    # requests, without requiring a CSRF-token framework. SESSION_COOKIE_SECURE
    # is deliberately left at its default (False): this app is normally
    # reached over plain http:// on the LAN (see deploy.sh), and Secure=True
    # would silently stop the session cookie from being sent at all there,
    # locking everyone out of login - only set it if this is ever put behind
    # HTTPS.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.before_request
    def _require_setup():
        if app.config.get("JF_CONFIG") is None and request.endpoint not in _SETUP_EXEMPT_ENDPOINTS:
            return redirect(url_for("setup.setup_view"))
        return None

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
    # Without this, Jinja caches compiled templates indefinitely for as long
    # as the process runs (app.debug=False here, so auto_reload would
    # default to False): editing an .html file on the LXC without
    # restarting the service would never show up. Negligible cost (one mtime
    # check per render) for a lightly-used LAN admin tool - mainly avoids
    # wasting time debugging a "stale" page. Python code changes (.py)
    # always still require a service restart: Python never hot-reloads those.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    if cfg is not None:
        # Session key derived from the admin credentials: stable as long as
        # they don't change (no need for a dedicated env var), and
        # automatically invalidates open sessions if the password is changed.
        app.secret_key = hashlib.sha256(f"{cfg.admin_username}:{cfg.admin_password}".encode()).hexdigest()
    else:
        # No admin credentials exist yet (setup mode) - an ephemeral key is
        # fine, since there's nothing to authenticate until setup finishes
        # (which restarts the process and picks a stable key on the next run).
        app.secret_key = os.urandom(32).hex()

    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(setup_bp)

    poller = None
    if cfg is not None:
        # The poller is always instantiated (lets it be started/stopped from
        # the admin even if POLLER_ENABLED=false at boot), but only actually
        # runs at service startup if POLLER_ENABLED=true.
        poller = JellyfinPoller(cfg)
        if cfg.poller_enabled:
            poller.start()
    app.config["JF_POLLER"] = poller

    return app
