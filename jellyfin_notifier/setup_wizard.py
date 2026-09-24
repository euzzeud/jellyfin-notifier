"""First-run setup wizard and the "Configuration" admin page: lets .env be
created (when missing/incomplete - the app boots in a limited mode and
every other page redirects here until the required keys are filled in) and
edited afterwards (once logged in, like any other admin page) from the web
interface, instead of requiring SSH + a text editor on the server. The
settings.json backup/restore cards (see admin.py's settings_export/
settings_import) also render on this same page/template - the admin
thinks of both as "the configuration", even though they're two separate
files with very different sensitivity (.env holds credentials,
settings.json doesn't - see import_env_file()'s docstring in env_setup.py).

Saving writes .env then restarts the process in place (os.execv, after a
short delay so the HTTP response has time to reach the browser) so the new
values are picked up immediately - no manual `systemctl restart` needed."""

from __future__ import annotations

import hmac
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from . import env_setup, service_control
from .api_console import run_request
from .config import Config
from .pending import clear_pending
from .settings import Settings, save_settings
from .upcoming import clear_upcoming

setup_bp = Blueprint("setup", __name__)
logger = logging.getLogger(__name__)

# The two outgoing HTML email templates (see email_sender.py's TEMPLATES_DIR
# / admin.py's EMAIL_TEMPLATE_PATH / EMAIL_UPCOMING_TEMPLATE_PATH) can be
# edited in place from the admin's raw template editor - _DEFAULT_TEMPLATES_DIR
# holds an untouched copy of what ships with this install (never written to
# by that editor), used by factory_reset() below to restore them.
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_DEFAULT_TEMPLATES_DIR = _TEMPLATES_DIR / "defaults"
_RESETTABLE_TEMPLATES = ("email.html", "email_upcoming.html")

# Groups the flat field list from env_setup.field_spec() into a lighter,
# guided multi-step flow (Previous/Next) instead of one long form - each
# step covers one decision the admin actually has to make. "advanced" is
# deliberately last and marked optional: every key in it already has a
# sane default in .env.example, so a first-time install can go straight
# to Review without touching it. Any key that shows up in .env.example but
# isn't listed in a step's `keys` below still gets shown (appended to
# "advanced") rather than silently dropped, so a future .env.example
# addition can't disappear from the wizard.
_STEP_DEFS = [
    {"id": "admin", "title": "Account", "keys": ["ADMIN_USERNAME", "ADMIN_PASSWORD"]},
    # Also named "Jellyfin Connector" on the admin nav's api_console.html
    # page (live URL/API key overrides + a raw API request console) - same
    # name everywhere for what's conceptually the same setting, even
    # though this step and that page aren't the same code.
    {"id": "jellyfin", "title": "Jellyfin Connector", "keys": ["JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_PUBLIC_URL"]},
    {
        "id": "mail", "title": "Mail server",
        "keys": [
            "SMTP_HOST", "SMTP_PORT", "SMTP_ENCRYPTION", "SMTP_USERNAME", "SMTP_PASSWORD",
            "SENDER_EMAIL", "SENDER_NAME", "NOTIFY_RECIPIENTS",
        ],
    },
    {
        "id": "advanced", "title": "Advanced", "optional": True,
        "keys": [
            "DEBOUNCE_SECONDS", "NOTIFY_ITEM_TYPES", "WEBHOOK_SHARED_SECRET", "PORT",
            "POLLER_ENABLED", "POLL_INTERVAL_SECONDS", "SEEN_ITEMS_PATH",
            "SETTINGS_PATH", "PENDING_ITEMS_PATH", "UPCOMING_PATH",
        ],
    },
]

# Fields rendered as a tag/chip input (comma-separated list) instead of a
# plain text box - the mail recipients list and the item-type filter, same
# widget already used for item-type overrides on the dashboard (setup.html
# picks the right validation/suggestions per key, see its taglist- loop).
_TAG_LIST_KEYS = {"NOTIFY_RECIPIENTS", "NOTIFY_ITEM_TYPES"}


def _build_steps(fields: list[dict]) -> list[dict]:
    by_key = {f["key"]: f for f in fields}
    used: set[str] = set()
    steps = []
    for step_def in _STEP_DEFS:
        step_fields = []
        for key in step_def["keys"]:
            field = by_key.get(key)
            if field is None:
                continue
            field = {**field, "is_tag_list": field["key"] in _TAG_LIST_KEYS}
            step_fields.append(field)
            used.add(key)
        steps.append({"id": step_def["id"], "title": step_def["title"], "optional": step_def.get("optional", False), "fields": step_fields})

    # Any field not covered by a step above (e.g. a new .env.example key)
    # lands in "advanced" rather than being dropped from the wizard.
    leftover = [f for f in fields if f["key"] not in used]
    if leftover:
        for step in steps:
            if step["id"] == "advanced":
                step["fields"].extend({**f, "is_tag_list": f["key"] in _TAG_LIST_KEYS} for f in leftover)
                break

    steps.append({"id": "review", "title": "Review & save", "optional": False, "fields": []})
    return steps


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
    """Restarts the process in place so the just-written .env takes effect
    immediately. os.execv() replaces the running process image (same PID,
    fresh Python interpreter, re-runs run.py from scratch) - it works
    without any process manager, which matters since this has to work the
    same way in local dev (a bare `python run.py`, no systemd) as it does
    in production (systemd).

    If os.execv() itself fails for any reason (a bad interpreter path, a
    permission quirk, anything) this used to fail COMPLETELY SILENTLY: the
    exception just died inside this daemon thread with nothing surfaced to
    the admin, and - critically - the running process never actually
    restarts. Config/JF_CONFIG is only ever computed once, at import time
    in run.py (`app = create_app()` at module level) - so with no restart,
    the process keeps serving forever with whatever config it booted with,
    even though the .env on disk was correctly updated. The admin sees
    "Saved, restarting..." and then lands right back on /setup no matter
    how many times they fill the form correctly - there's no way to tell
    the difference between "still filling it in wrong" and "the restart
    itself never happened" without this logging.

    Now: any execv failure is logged loudly (shows up in `journalctl -u
    jellyfin-notifier` / the Logs page), and as a fallback the process
    hard-exits - systemd's Restart=on-failure (see jellyfin-notifier.service)
    then spawns a genuinely fresh process that re-reads .env from disk, so
    a broken execv still recovers under systemd. In local dev (no systemd),
    a failed execv now visibly exits instead of limping along silently -
    the service just needs to be started again by hand, which is at least
    obvious instead of mysterious."""
    def _do_restart():
        time.sleep(delay)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            logger.exception(
                "Restart failed (os.execv raised) - the process is still running with its OLD "
                "configuration even though .env was written correctly. Exiting so a process "
                "manager (systemd's Restart=on-failure) can start a genuinely fresh process; "
                "if nothing restarts this after a few seconds, restart the service by hand."
            )
            os._exit(1)

    threading.Thread(target=_do_restart, daemon=True).start()


def _clear_loaded_env_vars() -> None:
    """Removes every jellyfin-notifier .env key from THIS process' own
    os.environ - called right before a reset-and-restart that deletes the
    .env file (setup_reset()/factory_reset()). Deleting the file alone
    isn't enough: _schedule_restart()'s os.execv() re-execs this same
    process image, which inherits its CURRENT os.environ as-is - any key
    already loaded at boot (env_setup.load_into_environ(), or systemd's
    EnvironmentFile=, before this very process even started) would
    otherwise still be sitting there after the "restart", so
    Config.from_env() would succeed again from those stale values even
    though the file is gone - the reset LOOKS like it worked (file
    deleted, page reloads) but silently drops right back into the old
    configuration instead of the setup wizard."""
    for field in env_setup.field_spec():
        os.environ.pop(field["key"], None)


def _fields_for(env_path: Path) -> list[dict]:
    """Builds the field list the setup form renders from. Deliberately
    never pre-fills a field with one of .env.example's fill-in-the-blank
    placeholder values (env_setup.PLACEHOLDER_VALUES - "smtp.example.com",
    "your-address@example.com", "someone@example.com", the fake Jellyfin
    IPs, "change-me"): those exist only to show the expected shape in
    .env.example, not as something a real install could legitimately keep.
    Pre-filling them looked like a real value already typed in, which is
    exactly how they used to slip through unresolved_keys() and get saved
    verbatim (see env_setup.PLACEHOLDER_VALUES's own docstring). A field
    like that is shown empty instead, with the placeholder text as a
    grayed-out input hint (`placeholder_hint`) so the expected format is
    still visible. Genuinely usable defaults (PORT=5005,
    NOTIFY_ITEM_TYPES=Movie,Series...) are unaffected and still pre-filled.

    `has_saved_value` gets the same placeholder-aware treatment for secret
    fields: a .env that still holds the literal example password
    (SMTP_PASSWORD=xxxxxxxxxxxxxxxx - e.g. from a .env.example copied
    as-is, or a previous incomplete save) is NOT "saved" in any meaningful
    sense. Counting it as saved made the field show "•••••••• (saved,
    leave empty to keep it)" and skip the required-field check on the
    review step - the admin, seeing "already saved", would leave it blank
    and get bounced right back to the same unresolved_keys() error forever,
    with no indication *why* it kept failing."""
    existing = env_setup.parse_env_file(env_path)
    fields = []
    for field in env_setup.field_spec():
        saved = existing.get(field["key"])
        if field["secret"]:
            value, placeholder_hint = "", ""
            is_placeholder = saved is not None and saved == env_setup.PLACEHOLDER_VALUES.get(field["key"])
            has_saved_value = bool(saved) and not is_placeholder
        else:
            value = saved if saved is not None else field["default"]
            placeholder_hint = ""
            if value == env_setup.PLACEHOLDER_VALUES.get(field["key"]):
                value, placeholder_hint = "", value
            has_saved_value = bool(saved)
        fields.append({
            **field,
            "value": value,
            "placeholder_hint": placeholder_hint,
            "has_saved_value": has_saved_value,
        })
    return fields


@setup_bp.route("/setup", methods=["GET"])
def setup_view():
    env_path = _env_path()
    configured = current_app.config.get("JF_CONFIG") is not None
    fields = _fields_for(env_path)

    return render_template(
        "admin/setup.html",
        active="setup",
        bootstrap_mode=not configured,
        configured=configured,
        fields=fields,
        steps=_build_steps(fields),
        # Shown on the review step so it's the last thing the admin sees
        # before saving - None when systemd isn't even available (nothing
        # to install), or when this is already running as the systemd
        # service (nothing to do).
        systemd_install_command=(
            None if service_control.is_unit_installed() else service_control.get_systemd_install_command()
        ),
        setup_error=current_app.config.get("JF_SETUP_ERROR"),
        # NOT the same thing as setup_error being set: JF_SETUP_ERROR stays
        # set on every plain GET for as long as the boot-time config is
        # incomplete, whereas just_attempted means "a save was JUST
        # submitted and rejected" - only the latter should skip the landing
        # screen straight to the wizard (see setup_save()'s error branch).
        # Conflating the two used to skip the landing page on every single
        # visit while .env was incomplete, so it never actually showed.
        just_attempted=False,
        env_exists=env_path.exists(),
        saved=request.args.get("saved") == "1",
        # settings.json's own backup/restore/reset flashes (admin.py's
        # settings_reset()/settings_import() redirect here, not to the
        # dashboard, now that both halves of "configuration" - .env AND
        # settings.json - live on this one page).
        settings_reset=request.args.get("settings_reset") == "1",
        imported=request.args.get("imported") == "1",
        import_error=request.args.get("import_error"),
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
    env_setup.reload_into_environ(env_path)

    try:
        Config.from_env()
        error = None
    except Exception as exc:
        error = str(exc)

    if not error:
        # Config.from_env() only checks presence, not that a value is real -
        # a save that leaves e.g. SMTP_PASSWORD=xxxxxxxxxxxxxxxx or
        # ADMIN_PASSWORD=change-me untouched would otherwise be accepted as
        # "done" and silently boot into a broken, insecure install.
        unresolved = env_setup.unresolved_keys(os.environ)
        if unresolved:
            error = "Still using the example value for: " + ", ".join(unresolved) + " - replace it with a real value."

    if error:
        fields = _fields_for(env_path)
        return render_template(
            "admin/setup.html",
            active="setup",
            bootstrap_mode=current_app.config.get("JF_CONFIG") is None,
            configured=current_app.config.get("JF_CONFIG") is not None,
            fields=fields,
            steps=_build_steps(fields),
            setup_error=f"Saved, but the configuration is still incomplete: {error}",
            just_attempted=True,
            env_exists=env_path.exists(),
            saved=False,
        )

    _schedule_restart()
    return render_template("admin/setup_restarting.html")


@setup_bp.route("/setup/reset", methods=["POST"])
def setup_reset():
    """"Start over" escape hatch for a first-run setup that's gone wrong
    (a bad partial save, wrong values, whatever) - wipes the current .env
    entirely and restarts, which drops straight back into a blank wizard
    (create_app() only reaches setup mode when no valid .env exists).

    ONLY reachable while still in bootstrap_mode (no valid config yet) -
    setup.html no longer renders this button once the app is actually
    configured, and this is the server-side half of that: wiping every
    credential (admin login, SMTP, Jellyfin API key) behind nothing but a
    confirm popup and the session cookie, no password re-entry, was fine
    pre-auth (there's no working login to check a password against yet
    anyway) but not once a real install exists - the URL itself must not
    become the bypass for hiding the button. "Reset everything" (Danger
    Zone's factory_reset() below) is the equivalent for a configured
    install, and it does require typing the admin password.

    Only wipes .env - settings.json, the pending queue, the "upcoming"
    titles/posters, the poller's "already seen" state and the email
    templates are all left untouched. For the version that wipes
    everything, see factory_reset() below (the Danger Zone's own button)."""
    if current_app.config.get("JF_CONFIG") is not None:
        return redirect(url_for("setup.setup_view"))

    env_path = _env_path()
    env_path.unlink(missing_ok=True)
    _clear_loaded_env_vars()
    _schedule_restart()
    return render_template(
        "admin/setup_restarting.html",
        heading="Configuration reset — restarting…",
        subtext="This page will reload automatically in a few seconds, into a blank setup.",
    )


def _restore_default_templates() -> None:
    """Restores email.html/email_upcoming.html to the version shipped with
    this install (_DEFAULT_TEMPLATES_DIR - never written to by the admin's
    raw template editor, which only ever touches the live email.html/
    email_upcoming.html directly). Takes a timestamped backup first, same
    pattern as a normal template save (_save_raw_template() in admin.py),
    so a customized version isn't lost forever, just superseded - it's
    still sitting in templates/backups/ afterwards."""
    backup_dir = _TEMPLATES_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in _RESETTABLE_TEMPLATES:
        live_path = _TEMPLATES_DIR / name
        default_path = _DEFAULT_TEMPLATES_DIR / name
        if not default_path.exists():
            # Shouldn't happen in a normal install (both ship in the
            # package) - skip rather than fail the whole reset over one
            # missing reference file.
            continue
        if live_path.exists():
            shutil.copy2(live_path, backup_dir / f"{live_path.stem}.{stamp}.html.bak")
        shutil.copy2(default_path, live_path)


@setup_bp.route("/setup/factory-reset", methods=["POST"])
def factory_reset():
    """The Danger Zone's own "Reset everything" button - deliberately much
    broader than setup_reset() ("Start over", .env only) or a plain
    settings.json reset: wipes EVERY piece of state this app owns in one
    shot - the .env credentials, settings.json, the pending mail queue,
    the "upcoming" titles and their posters, the poller's "already seen"
    state, and the two HTML email templates (restored to what ships with
    this install) - leaving the install exactly like a fresh, never-
    configured one. Ends the same way setup_reset() does: .env is gone, so
    the restart drops straight into a blank first-run wizard - there is no
    longer any config left to redirect to, or log into, otherwise.

    Gated behind the same password re-authentication as a .env import
    (see setup_import()) - if any single action here deserves asking
    twice, it's the one that deletes everything at once."""
    cfg = current_app.config.get("JF_CONFIG")
    env_path = _env_path()

    if cfg is None:
        # The Danger Zone card is only ever rendered once configured
        # (see setup.html), so this shouldn't be reachable - but if it
        # somehow is (e.g. a stale/bookmarked form), there's no admin
        # account yet to check a password against and nothing configured
        # to reset, so just send them to the wizard instead of guessing.
        return redirect(url_for("setup.setup_view"))

    confirm_password = request.form.get("confirm_password", "")
    if not confirm_password or not hmac.compare_digest(confirm_password, cfg.admin_password):
        return _import_rejected(env_path, "Incorrect password - nothing was reset.")

    save_settings(cfg.settings_path, Settings())
    clear_pending(cfg.pending_items_path)
    clear_upcoming(cfg.upcoming_path)
    Path(cfg.seen_items_path).unlink(missing_ok=True)
    _restore_default_templates()
    env_path.unlink(missing_ok=True)
    _clear_loaded_env_vars()
    logger.warning(
        "Factory reset: .env, settings.json, pending queue, upcoming titles, "
        "poller seen-state and email templates were all wiped/restored to defaults."
    )

    _schedule_restart()
    return render_template(
        "admin/setup_restarting.html",
        heading="Everything reset — restarting…",
        subtext="This page will reload automatically in a few seconds, into a blank setup.",
    )


@setup_bp.route("/setup/test-jellyfin", methods=["POST"])
def setup_test_jellyfin():
    """Live connectivity test for the Jellyfin URL/API key currently typed
    into the form - nothing is saved. This is what makes the wizard actually
    guide the admin: a wrong IP/port is caught right here, instead of only
    surfacing later as a red error on the dashboard once the poller starts
    trying (and failing) to reach it every cycle."""
    payload = request.get_json(silent=True) or {}
    url = (payload.get("jellyfin_url") or "").strip()
    api_key = (payload.get("jellyfin_api_key") or "").strip()
    if not url:
        return jsonify({"ok": False, "message": "Enter a Jellyfin URL first."})

    # /System/Info/Public doesn't require an API key, so this still gives a
    # useful answer ("the server is reachable") even before the admin has
    # typed the key in - and a wrong/missing key on the real server would
    # only cause 401s on other endpoints later, not a connection failure.
    result = run_request(url, api_key, "GET", "/System/Info/Public", "")
    if result["ok"]:
        body = result["body"] if isinstance(result["body"], dict) else {}
        name = body.get("ServerName") or "Jellyfin"
        version = body.get("Version")
        suffix = f" (v{version})" if version else ""
        return jsonify({"ok": True, "message": f'Connected to "{name}"{suffix}.'})
    return jsonify({"ok": False, "message": f"Could not reach {url}: {result['body']}"})


def _import_rejected(env_path: Path, message: str):
    fields = _fields_for(env_path)
    return render_template(
        "admin/setup.html",
        active="setup",
        bootstrap_mode=current_app.config.get("JF_CONFIG") is None,
        configured=current_app.config.get("JF_CONFIG") is not None,
        fields=fields,
        steps=_build_steps(fields),
        setup_error=message,
        just_attempted=True,
        env_exists=env_path.exists(),
        saved=False,
    )


@setup_bp.route("/setup/import", methods=["POST"])
def setup_import():
    upload = request.files.get("env_file")
    if not upload or not upload.filename:
        return redirect(url_for("setup.setup_view"))

    env_path = _env_path()

    # Re-authentication gate: importing overwrites the ENTIRE configuration
    # in one shot, including ADMIN_USERNAME/ADMIN_PASSWORD and every other
    # credential - a much bigger blast radius than editing one field, so it
    # asks for the password again even though the session is already
    # authenticated (_setup_auth's normal login check already ran). Only
    # meaningful once an admin account actually exists to check against -
    # during first-run bootstrap (JF_CONFIG is None) there's no config, no
    # ADMIN_PASSWORD to compare against, and nothing yet worth protecting.
    cfg = current_app.config.get("JF_CONFIG")
    if cfg is not None:
        confirm_password = request.form.get("confirm_password", "")
        if not confirm_password or not hmac.compare_digest(confirm_password, cfg.admin_password):
            return _import_rejected(env_path, "Incorrect password - the .env file was not imported.")

    text = upload.read().decode("utf-8", errors="replace")
    ok, error = env_setup.import_env_file(env_path, text)
    if not ok:
        return _import_rejected(env_path, f"Could not import {upload.filename!r}: {error}")

    env_setup.reload_into_environ(env_path)
    _schedule_restart()
    return render_template("admin/setup_restarting.html")
