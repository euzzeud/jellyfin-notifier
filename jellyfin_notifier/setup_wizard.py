"""First-run setup wizard and the "Environment" admin page: lets .env be
created (when missing/incomplete - the app boots in a limited mode and
every other page redirects here until the required keys are filled in) and
edited afterwards (once logged in, like any other admin page) from the web
interface, instead of requiring SSH + a text editor on the server.

Saving writes .env then restarts the process in place (os.execv, after a
short delay so the HTTP response has time to reach the browser) so the new
values are picked up immediately - no manual `systemctl restart` needed."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from . import env_setup
from .config import Config

setup_bp = Blueprint("setup", __name__)


def _env_path() -> Path:
    return current_app.config.get("JF_ENV_PATH") or Path(".env")


@setup_bp.before_request
def _setup_auth():
    # Config already valid: this is the "edit later" path, gated behind the
    # normal admin login like every other admin page. While no valid config
    # exists yet (first run, or a botched edit), the wizard has to stay
    # reachable without logging in - there's no admin account to log in with.
    if current_app.config.get("JF_CONFIG") is not None:
        if not session.get("authenticated"):
            return redirect(url_for("admin.login", next=request.path))
    return None


def _schedule_restart(delay: float = 1.2) -> None:
    def _do_restart():
        time.sleep(delay)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()


@setup_bp.route("/setup", methods=["GET"])
def setup_view():
    env_path = _env_path()
    configured = current_app.config.get("JF_CONFIG") is not None
    existing = env_setup.parse_env_file(env_path)
    fields = []
    for field in env_setup.field_spec():
        value = "" if field["secret"] else existing.get(field["key"], field["default"])
        fields.append({**field, "value": value, "has_saved_value": bool(existing.get(field["key"]))})

    return render_template(
        "admin/setup.html",
        active="setup",
        bootstrap_mode=not configured,
        configured=configured,
        fields=fields,
        setup_error=current_app.config.get("JF_SETUP_ERROR"),
        env_exists=env_path.exists(),
        saved=request.args.get("saved") == "1",
    )


@setup_bp.route("/setup", methods=["POST"])
def setup_save():
    env_path = _env_path()
    submitted: dict[str, str] = {}
    for field in env_setup.field_spec():
        raw = request.form.get(field["key"])
        if raw is None:
            continue
        if field["secret"] and not raw.strip():
            # Blank secret field = "keep the current value" (never
            # pre-filled back into the form, so blank doesn't mean "clear
            # it" - it means "wasn't touched").
            continue
        submitted[field["key"]] = raw.strip()

    env_setup.write_env_file(env_path, submitted)
    env_setup.load_into_environ(env_path)

    try:
        Config.from_env()
        error = None
    except Exception as exc:
        error = str(exc)

    if error:
        existing = env_setup.parse_env_file(env_path)
        fields = []
        for field in env_setup.field_spec():
            value = "" if field["secret"] else existing.get(field["key"], field["default"])
            fields.append({**field, "value": value, "has_saved_value": bool(existing.get(field["key"]))})
        return render_template(
            "admin/setup.html",
            active="setup",
            bootstrap_mode=current_app.config.get("JF_CONFIG") is None,
            configured=current_app.config.get("JF_CONFIG") is not None,
            fields=fields,
            setup_error=f"Saved, but the configuration is still incomplete: {error}",
            env_exists=env_path.exists(),
            saved=False,
        )

    _schedule_restart()
    return render_template("admin/setup_restarting.html")


@setup_bp.route("/setup/import", methods=["POST"])
def setup_import():
    upload = request.files.get("env_file")
    if not upload or not upload.filename:
        return redirect(url_for("setup.setup_view"))

    env_path = _env_path()
    text = upload.read().decode("utf-8", errors="replace")
    ok, error = env_setup.import_env_file(env_path, text)
    if not ok:
        existing = env_setup.parse_env_file(env_path)
        fields = []
        for field in env_setup.field_spec():
            value = "" if field["secret"] else existing.get(field["key"], field["default"])
            fields.append({**field, "value": value, "has_saved_value": bool(existing.get(field["key"]))})
        return render_template(
            "admin/setup.html",
            active="setup",
            bootstrap_mode=current_app.config.get("JF_CONFIG") is None,
            configured=current_app.config.get("JF_CONFIG") is not None,
            fields=fields,
            setup_error=f"Could not import {upload.filename!r}: {error}",
            env_exists=env_path.exists(),
            saved=False,
        )

    env_setup.load_into_environ(env_path)
    _schedule_restart()
    return render_template("admin/setup_restarting.html")
