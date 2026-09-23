"""Web admin interface (/admin): notification schedule, "CMS" template
editor, next email preview, upcoming title announcements, ad-hoc Jellyfin
API console, and service control (start/stop/restart + logs).

Protected by HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD in .env) -
intended for LAN-only access (not exposed to the internet)."""

from __future__ import annotations

import hashlib
import hmac
import html.parser
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from jinja2 import Environment as JinjaEnv
from jinja2 import TemplateSyntaxError

from . import mail_history, metrics, service_control
from .api_console import run_request
from .config import SMTP_ENCRYPTIONS
from .email_blocks import BLOCK_TYPES, compile_blocks_to_html, resolve_blocks_json
from .email_sender import TEMPLATES_DIR, render_preview_html, send_email, send_test_email
from .jellyfin_client import JellyfinClient
from .pending import load_pending
from .poller import DEFAULT_POLLER_LIMIT
from .schedule import WEEKDAY_NAMES_EN, is_within_window, next_allowed_datetime
from .settings import Settings, load_settings, save_settings
from .upcoming import ALLOWED_POSTER_EXTENSIONS, UPLOADS_DIR, add_upcoming, delete_upcoming, get_many, item_from_upcoming, list_upcoming, save_poster

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)

EMAIL_TEMPLATE_PATH = TEMPLATES_DIR / "email.html"
EMAIL_UPCOMING_TEMPLATE_PATH = TEMPLATES_DIR / "email_upcoming.html"
ASSETS_DIR = Path(__file__).parent / "assets"

_FIELD_PREFIX = {"new": "", "upcoming": "upcoming_"}
_SIMPLE_FIELDS = (
    "template_subject_single", "template_subject_multi",
    "template_intro_single", "template_intro_multi", "template_footer",
    "color_bg", "color_card", "color_header", "color_accent", "color_button", "color_text", "color_muted",
)

FAKE_PREVIEW_ITEMS = [
    {
        "item_id": None,
        "item_type": "Movie",
        "name": "Terminator",
        "year": 1984,
        "overview": (
            "In Los Angeles in 1984, a Terminator, a cyborg from the future, is "
            "on a mission to kill Sarah Connor, a young woman whose unborn "
            "child will one day save humanity."
        ),
        "genres": ["Action", "Science Fiction"],
        "community_rating": 7.5,
        "run_time_ticks": 63_000_000_000,
        "type_label": "Movie",
        "_fake_deep_link": "#preview",
    },
]

# Separate example item for the "Upcoming" tab preview (as long as no title
# has been added to the list yet) - deliberately different from
# FAKE_PREVIEW_ITEMS so the two previews don't look alike.
FAKE_UPCOMING_PREVIEW_ITEMS = [
    {
        "item_id": None,
        "item_type": "Movie",
        "name": "Dune: Part Three",
        "year": None,
        "overview": "Coming soon to Jellyfin.",
        "genres": None,
        "community_rating": None,
        "run_time_ticks": None,
        "type_label": "Coming soon • Movie",
    },
]


def _config():
    return current_app.config["JF_CONFIG"]


def _poller():
    return current_app.config.get("JF_POLLER")


@admin_bp.context_processor
def _inject_header_context():
    """Makes the Jellyfin URL (.env) available in ALL admin templates -
    displayed at the top right of the header. Previously the template read
    `config.jellyfin_url` (Flask's own config, not our Config class): that
    didn't raise an error thanks to Jinja's "silent" rendering of an
    undefined value, but never displayed anything either.
    Deliberately uses `cfg.jellyfin_url` (already in memory, from .env)
    rather than any Settings override: this context processor runs on EVERY
    template render, so there's no question of adding one more JSON
    read+parse per page just for a cosmetic display - the pages that
    actually need the effective value (with override) already compute it
    themselves (see mail_server_view, api_console_view)."""
    try:
        cfg = _config()
        return {"jellyfin_url": cfg.jellyfin_url if cfg else ""}
    except (RuntimeError, KeyError):
        return {}


# ---------------------------------------------------------------------------
# Static files (logo displayed in the admin header)
# ---------------------------------------------------------------------------

@admin_bp.route("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(ASSETS_DIR, filename)


# ---------------------------------------------------------------------------
# HTML validation: Jinja validation alone (compile + render the template)
# does NOT catch any HTML markup problem - a malformed attribute like
# `<html =d1d1>` is perfectly valid Jinja/text, so it "compiles" without
# error. So a lightweight structural check (unclosed/mismatched tags,
# malformed attributes) is added on top of the Jinja check.
# ---------------------------------------------------------------------------

_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Valid attribute: name (letters/digits/-/:/_ , must start with a letter)
# optionally followed by ="value"/'value'/value. An attribute like `=d1d1`
# (empty name, starts with "=") doesn't match -> flagged.
_ATTR_RE = re.compile(
    r'\s+([a-zA-Z][a-zA-Z0-9\-:_]*)(\s*=\s*("[^"]*"|\'[^\']*\'|[^\s"\'=<>`]+))?'
)


def _check_html_structure(source: str) -> str | None:
    """Returns an error message (or None if nothing was detected). Ignores
    Jinja blocks ({% ... %}, {{ ... }}) so they aren't mistaken for HTML."""
    # Neutralizes Jinja blocks so they don't confuse the HTML parser (e.g.
    # {% for item in items %} implicitly contains `<`/`>` via the generated
    # text, but mostly we don't want the tag-balancer to get confused).
    cleaned = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", "", source, flags=re.DOTALL)

    errors: list[str] = []
    stack: list[tuple[str, int]] = []

    class _Checker(html.parser.HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in _VOID_ELEMENTS:
                stack.append((tag, self.getpos()[0]))

        def handle_startendtag(self, tag, attrs):
            pass  # self-closing (<br />): nothing to push onto the stack

        def handle_endtag(self, tag):
            if tag in _VOID_ELEMENTS:
                return
            if not stack:
                errors.append(f"line {self.getpos()[0]}: closing tag </{tag}> has no matching opening tag")
                return
            # Looks for the matching opening tag in the stack (handles
            # mismatched nesting like <a><b></a></b>)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    unclosed = stack[i + 1:]
                    if unclosed:
                        names = ", ".join(f"<{n}>" for n, _ in unclosed)
                        errors.append(f"line {self.getpos()[0]}: {names} not closed before </{tag}>")
                    del stack[i:]
                    return
            errors.append(f"line {self.getpos()[0]}: </{tag}> does not match any open tag")

        def error(self, message):  # required by some versions of html.parser
            errors.append(message)

    parser = _Checker(convert_charrefs=True)
    try:
        parser.feed(cleaned)
        parser.close()
    except Exception as exc:  # pragma: no cover - safety net
        return f"HTML parsing error: {exc}"

    if stack:
        names = ", ".join(f"<{n}> (line {ln})" for n, ln in stack)
        errors.append(f"unclosed tag(s): {names}")

    # Checks for malformed attributes in every opening tag (e.g. <html =d1d1>)
    for m in re.finditer(r"<([a-zA-Z][a-zA-Z0-9\-:_]*)((?:\s+[^<>]*?)?)\s*/?>", cleaned):
        tag, attr_blob = m.group(1), m.group(2)
        if not attr_blob.strip():
            continue
        consumed = _ATTR_RE.sub("", attr_blob)
        leftover = consumed.strip()
        if leftover:
            line = cleaned[: m.start()].count("\n") + 1
            errors.append(f"line {line}: malformed attribute in <{tag}> near « {leftover[:30]} »")

    return errors[0] if errors else None


_PUBLIC_ENDPOINTS = {"admin.login", "admin.assets"}


@admin_bp.before_request
def _require_auth():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if not session.get("authenticated"):
        return redirect(url_for("admin.login", next=request.path))
    return None


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    cfg = _config()
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username, cfg.admin_username) and hmac.compare_digest(password, cfg.admin_password):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            next_url = request.form.get("next") or url_for("admin.dashboard")
            # Minimal safety: only allows internal redirects (avoids an open redirect).
            if not next_url.startswith("/"):
                next_url = url_for("admin.dashboard")
            logger.info("Login successful (user=%s, from=%s).", username, request.remote_addr)
            metrics.inc_login(True)
            return redirect(next_url)
        error = "Invalid username or password."
        logger.warning("Failed login attempt (user=%r, from=%s).", username, request.remote_addr)
        metrics.inc_login(False)

    return render_template("admin/login.html", error=error, next=request.args.get("next", ""))


@admin_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    logger.info("Logout.")
    return redirect(url_for("admin.login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@admin_bp.route("/", methods=["GET", "POST"])
def dashboard():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    poller = _poller()
    poller_saved = False

    if request.method == "POST" and request.form.get("action") == "save_poller":
        settings.poller_item_types_override = request.form.get("poller_item_types_override", "").strip()
        settings.poller_interval_seconds_override = request.form.get("poller_interval_seconds_override", type=int) or 0
        settings.poller_limit_override = request.form.get("poller_limit_override", type=int) or 0
        save_settings(cfg.settings_path, settings)
        poller_saved = True
        logger.info("Poller settings saved (interval/limit/item types override).")

    poll_status = poller.status() if poller else None
    service_status = service_control.get_status()
    pending = load_pending(cfg.pending_items_path)
    logs_tail = service_control.get_logs(40)

    return render_template(
        "admin/dashboard.html",
        active="dashboard",
        settings=settings,
        poll_status=poll_status,
        service_status=service_status,
        pending=pending,
        logs_tail=logs_tail,
        within_window=is_within_window(settings),
        next_slot=next_allowed_datetime(settings),
        poller_saved=poller_saved,
        default_item_types=",".join(sorted(cfg.notify_item_types)),
        default_interval=cfg.poll_interval_seconds,
        default_limit=DEFAULT_POLLER_LIMIT,
    )


# ---------------------------------------------------------------------------
# Reset settings (settings.json) to the code's default values - email
# text/colors, schedule, poller/Jellyfin/SMTP overrides. Does NOT touch the
# data (pending queue, "upcoming" titles + posters, poller "already seen"
# state) nor the edited raw HTML templates (email.html / email_upcoming.html,
# which have their own backups in templates/backups/).
# ---------------------------------------------------------------------------

@admin_bp.route("/settings/reset", methods=["POST"])
def settings_reset():
    cfg = _config()
    save_settings(cfg.settings_path, Settings())
    logger.warning("All notifier settings reset to their defaults.")
    # Same "Configuration" page as settings_import() above, not the
    # dashboard - see its comment.
    return redirect(url_for("setup.setup_view", settings_reset=1))


# ---------------------------------------------------------------------------
# Poller control (start/stop/pause/resume) from the admin
# ---------------------------------------------------------------------------

@admin_bp.route("/poller/<action>", methods=["POST"])
def poller_action(action: str):
    cfg = _config()
    poller = _poller()
    if poller is None:
        return jsonify({"ok": False, "error": "Poller not available."}), 400

    if action == "start":
        poller.start()
    elif action == "stop":
        poller.stop()
    elif action == "pause":
        settings = load_settings(cfg.settings_path)
        settings.poller_paused = True
        save_settings(cfg.settings_path, settings)
    elif action == "resume":
        settings = load_settings(cfg.settings_path)
        settings.poller_paused = False
        save_settings(cfg.settings_path, settings)
    else:
        return jsonify({"ok": False, "error": "Unknown action."}), 400

    logger.info("Poller action: %s.", action)
    return jsonify({"ok": True, "status": poller.status()})


# ---------------------------------------------------------------------------
# "Already seen" items (seen_items.json) - lets the poller's dedup state be
# inspected and reset from the admin instead of editing the file by hand on
# the server.
# ---------------------------------------------------------------------------

@admin_bp.route("/poller/seen/data")
def poller_seen_data():
    poller = _poller()
    if poller is None:
        return jsonify({"count": 0, "ids": []})
    return jsonify({"count": poller.seen_count(), "ids": poller.seen_ids_sorted()})


@admin_bp.route("/poller/seen/export")
def poller_seen_export():
    poller = _poller()
    ids = poller.seen_ids_sorted() if poller else []
    payload = json.dumps(ids, indent=2)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger.info("Already-seen items exported (%d item id(s)).", len(ids))
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="jellyfin-notifier-seen-items-{stamp}.json"'},
    )


@admin_bp.route("/poller/seen/clear", methods=["POST"])
def poller_seen_clear():
    poller = _poller()
    if poller is None:
        return jsonify({"ok": False, "error": "Poller not available."}), 400
    poller.clear_seen()
    return jsonify({"ok": True, "count": poller.seen_count()})


@admin_bp.route("/poller/seen/rebootstrap", methods=["POST"])
def poller_seen_rebootstrap():
    poller = _poller()
    if poller is None:
        return jsonify({"ok": False, "error": "Poller not available."}), 400
    ok, error = poller.rebootstrap_seen()
    return jsonify({"ok": ok, "error": error, "count": poller.seen_count()})


# ---------------------------------------------------------------------------
# Service management (start/stop/restart) + logs
# ---------------------------------------------------------------------------

@admin_bp.route("/service/<action>", methods=["POST"])
def service_action_route(action: str):
    ok, output = service_control.service_action(action)
    (logger.info if ok else logger.error)("Service action: %s -> %s.", action, "ok" if ok else "failed")
    return jsonify({"ok": ok, "output": output, "status": service_control.get_status()})


@admin_bp.route("/service/transition/<action>")
def service_transition(action: str):
    # Stop/restart terminate this very process - it can't reliably finish
    # sending a normal JSON response once systemd signals it to exit, so the
    # confirmation dialog navigates here first: a standalone page that shows
    # a clear "stopped"/"restarting" message right away, fires the actual
    # action itself, and (for restart, and opportunistically for stop too,
    # in case the service is started again from outside the interface)
    # polls until the interface responds again and redirects automatically.
    if action not in ("stop", "restart"):
        return redirect(url_for("admin.dashboard"))
    return render_template(
        "admin/service_transition.html",
        action=action,
        service_name=service_control.SERVICE_NAME,
        action_url=url_for("admin.service_action_route", action=action),
    )


@admin_bp.route("/logs")
def logs():
    lines = request.args.get("lines", default=300, type=int)
    return render_template(
        "admin/logs.html",
        active="logs",
        logs=service_control.get_logs_structured(lines),
        lines=lines,
    )


@admin_bp.route("/logs/data")
def logs_data():
    lines = request.args.get("lines", default=300, type=int)
    return jsonify({"logs": service_control.get_logs_structured(lines), "service": service_control.get_status()})


# ---------------------------------------------------------------------------
# Schedule (allowed days / hours)
# ---------------------------------------------------------------------------

@admin_bp.route("/schedule", methods=["GET", "POST"])
def schedule_view():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    saved = False

    if request.method == "POST":
        days = [int(d) for d in request.form.getlist("notify_days")]
        settings.notify_days = days
        settings.notify_hour_start = request.form.get("notify_hour_start", settings.notify_hour_start)
        settings.notify_hour_end = request.form.get("notify_hour_end", settings.notify_hour_end)
        try:
            settings.overview_max_length = max(50, int(request.form.get("overview_max_length", settings.overview_max_length)))
        except ValueError:
            pass
        save_settings(cfg.settings_path, settings)
        saved = True
        logger.info("Schedule saved (days=%s, window=%s-%s).", days, settings.notify_hour_start, settings.notify_hour_end)

    return render_template(
        "admin/schedule.html",
        active="schedule",
        settings=settings,
        weekday_names=list(enumerate(WEEKDAY_NAMES_EN)),
        saved=saved,
        within_window=is_within_window(settings),
        next_slot=next_allowed_datetime(settings),
    )


# ---------------------------------------------------------------------------
# Template editor (simple fields + raw HTML editor with validation)
# ---------------------------------------------------------------------------

def _validate_template_source(source: str) -> tuple[bool, str]:
    env = JinjaEnv(autoescape=True)
    settings = Settings()
    try:
        template = env.from_string(source)
        template.render(
            items=[
                {
                    "name": "Example", "year": 2024, "overview": "Example synopsis.",
                    "type_label": "Movie", "genres": "Action", "rating": "7.5",
                    "duration": "1h47", "image_cid": None, "image_url": None, "deep_link": "#",
                }
            ],
            count=1,
            date="01/01/2026 12:00",
            logo_src=None,
            intro_text="New content is available!",
            footer_text="Automatically sent by your Jellyfin server",
            color_bg=settings.color_bg,
            color_card=settings.color_card,
            color_header=settings.color_header,
            color_accent=settings.color_accent,
            color_button=settings.color_button,
            color_text=settings.color_text,
            color_muted=settings.color_muted,
        )
    except TemplateSyntaxError as exc:
        return False, f"Syntax error on line {exc.lineno}: {exc.message}"
    except Exception as exc:
        return False, f"Render error: {exc}"

    html_error = _check_html_structure(source)
    if html_error:
        return False, f"Invalid HTML: {html_error}"

    return True, ""


@admin_bp.route("/new-content/validate", methods=["POST"])
def template_validate():
    source = (request.get_json(silent=True) or {}).get("source", "")
    valid, error = _validate_template_source(source)
    return jsonify({"valid": valid, "error": error})


def _save_simple_texts(scope: str, settings: Settings, cfg) -> None:
    prefix = _FIELD_PREFIX[scope]
    for field in _SIMPLE_FIELDS:
        value = request.form.get(field)
        if value:
            setattr(settings, prefix + field, value)
    save_settings(cfg.settings_path, settings)


def _save_raw_template(template_path: Path, raw_source: str) -> tuple[bool, str]:
    """Validates then saves a raw HTML template (with a timestamped
    backup). Returns (ok, error)."""
    valid, error = _validate_template_source(raw_source)
    if not valid:
        return False, error
    backup_dir = template_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(template_path, backup_dir / f"{template_path.stem}.{stamp}.html.bak")
    template_path.write_text(raw_source, encoding="utf-8")
    return True, ""


def _save_blocks(scope: str, settings: Settings, cfg, template_path: Path, blocks_json: str) -> tuple[bool, str]:
    """Compiles the visual editor's blocks into HTML/Jinja2 then reuses
    exactly the same save path (validation + timestamped backup) as the
    raw HTML editor - email_sender.py needs no changes at all."""
    try:
        blocks = json.loads(blocks_json or "[]")
        if not isinstance(blocks, list):
            raise ValueError("blocks must be a list")
    except (json.JSONDecodeError, ValueError) as exc:
        return False, f"Invalid block data: {exc}"

    compiled_html = compile_blocks_to_html(blocks)
    ok, error = _save_raw_template(template_path, compiled_html)
    if not ok:
        return False, error

    prefix = _FIELD_PREFIX[scope]
    setattr(settings, f"{prefix}email_blocks", json.dumps(blocks))
    save_settings(cfg.settings_path, settings)
    return True, ""


def _template_editor_view(scope: str, template_path: Path, active: str):
    """Generic editor view (text + colors + raw HTML) for "New Content
    Notifications" (scope="new") - each has its own text/colors
    (Settings.scoped) and its own template file."""
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    saved = None
    raw_error = None
    raw_source = template_path.read_text(encoding="utf-8")

    if request.method == "POST":
        form_type = request.form.get("form_type")

        if form_type == "toggle":
            settings.new_notifications_enabled = request.form.get("enabled") == "1"
            save_settings(cfg.settings_path, settings)
            logger.info("New Content Notifications module %s.", "enabled" if settings.new_notifications_enabled else "disabled")
            return redirect(url_for("admin.template_view"))

        elif form_type == "simple":
            _save_simple_texts(scope, settings, cfg)
            saved = "simple"
            logger.info("New Content template: texts/colors saved.")

        elif form_type == "raw":
            raw_source = request.form.get("raw_source", raw_source)
            valid, error = _save_raw_template(template_path, raw_source)
            saved = "raw" if valid else None
            raw_error = None if valid else error
            (logger.info if valid else logger.warning)(
                "New Content template: raw HTML %s.", "saved" if valid else f"rejected ({error})"
            )

        elif form_type == "blocks":
            valid, error = _save_blocks(scope, settings, cfg, template_path, request.form.get("blocks_json", "[]"))
            saved = "blocks" if valid else None
            raw_error = None if valid else error
            if valid:
                raw_source = template_path.read_text(encoding="utf-8")
            (logger.info if valid else logger.warning)(
                "New Content template: visual layout %s.", "saved" if valid else f"rejected ({error})"
            )

    prefix = _FIELD_PREFIX[scope]
    return render_template(
        "admin/template.html",
        active=active,
        wide_layout=True,
        scope=scope,
        settings=settings.scoped(scope),
        raw_source=raw_source,
        saved=saved,
        raw_error=raw_error,
        blocks_json=resolve_blocks_json(getattr(settings, f"{prefix}email_blocks", "")),
        block_types=BLOCK_TYPES,
        notifications_enabled=settings.new_notifications_enabled,
    )


def _template_live_preview(scope: str, template_name: str, fake_items: list[dict]):
    """Instant preview (nothing is saved): reflects the raw HTML AND the
    simple fields as currently typed in the form, not the version on
    disk."""
    cfg = _config()
    payload = request.get_json(silent=True) or {}
    settings = load_settings(cfg.settings_path).scoped(scope)

    for field in (
        "template_intro_single", "template_intro_multi", "template_footer",
        "color_bg", "color_card", "color_header", "color_accent", "color_button", "color_text", "color_muted",
    ):
        if payload.get(field):
            setattr(settings, field, payload[field])

    raw_source = payload.get("raw_source")
    if payload.get("blocks") is not None:
        try:
            raw_source = compile_blocks_to_html(payload.get("blocks") or [])
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Block error: {exc}"})

    valid, error = (True, "") if raw_source is None else _validate_template_source(raw_source)
    if not valid:
        return jsonify({"ok": False, "error": error})

    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    logo_url = url_for("admin.assets", filename="jellyfin-logo.png")
    try:
        html_out = render_preview_html(
            fake_items, client, settings, raw_source=raw_source, logo_url=logo_url, template_name=template_name
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Render error: {exc}"})
    return jsonify({"ok": True, "html": html_out})


@admin_bp.route("/new-content", methods=["GET", "POST"])
def template_view():
    return _template_editor_view("new", EMAIL_TEMPLATE_PATH, active="template")


@admin_bp.route("/new-content/live-preview", methods=["POST"])
def template_live_preview():
    return _template_live_preview("new", "email.html", FAKE_PREVIEW_ITEMS)


# ---------------------------------------------------------------------------
# Preview of the next mail
# ---------------------------------------------------------------------------

@admin_bp.route("/preview")
def preview():
    # The preview now lives directly in the "New Content Notifications" tab
    # (edit + preview side by side) - redirect the old URL. Note: the old
    # standalone /preview/frame endpoint this used to redirect to is gone
    # (dead code, superseded by the live-preview-in-tab redesign - see
    # template_live_preview above, which drives the same iframe today).
    return redirect(url_for("admin.template_view"))


# ---------------------------------------------------------------------------
# "Upcoming" titles (manual announcements) - same editor (text/colors/raw
# HTML + live preview) as "New Content Notifications", but scope="upcoming"
# and with the added management of manually uploaded posters.
# ---------------------------------------------------------------------------

def _poster_url(entry: dict) -> str | None:
    if not entry.get("poster_filename"):
        return None
    return url_for("admin.upcoming_poster", filename=entry["poster_filename"])


def _poster_disk_path(entry: dict) -> str | None:
    if not entry.get("poster_filename"):
        return None
    return str(UPLOADS_DIR / entry["poster_filename"])


def _upcoming_items_for_send(entries: list[dict]) -> list[dict]:
    """item_from_upcoming + injects the disk path of the uploaded poster
    (used by email_sender to attach it as cid: in the real mail)."""
    items = []
    for e in entries:
        item = item_from_upcoming(e)
        item["_local_poster_path"] = _poster_disk_path(e)
        items.append(item)
    return items


def _upcoming_items_for_preview(entries: list[dict]) -> list[dict]:
    """item_from_upcoming + injects the served URL of the uploaded poster
    (used for the browser preview)."""
    items = []
    for e in entries:
        item = item_from_upcoming(e)
        item["_poster_url"] = _poster_url(e)
        items.append(item)
    return items


@admin_bp.route("/upcoming/poster/<path:filename>")
def upcoming_poster(filename: str):
    return send_from_directory(UPLOADS_DIR, filename)


@admin_bp.route("/upcoming", methods=["GET", "POST"])
def upcoming_view():
    cfg = _config()
    sent = False
    saved = None
    raw_error = None
    raw_source = EMAIL_UPCOMING_TEMPLATE_PATH.read_text(encoding="utf-8")

    if request.method == "POST":
        action = request.form.get("action")
        form_type = request.form.get("form_type")

        if action == "add":
            name = request.form.get("name", "").strip()
            if not name:
                return redirect(url_for("admin.upcoming_view", add_error="Name is required."))

            entry = add_upcoming(
                cfg.upcoming_path,
                name=name,
                year=request.form.get("year", "").strip(),
                note=request.form.get("note", "").strip(),
                type_label=request.form.get("type_label", "Movie"),
            )
            poster = request.files.get("poster")
            if poster and poster.filename:
                stored = save_poster(cfg.upcoming_path, entry["id"], poster.filename, poster.read())
                if stored is None:
                    # save_poster() fails silently (disallowed extension) -
                    # previously, nothing reported this to the user, who
                    # just saw their title added WITHOUT the poster they
                    # had just uploaded, with no idea why.
                    allowed = ", ".join(sorted(ALLOWED_POSTER_EXTENSIONS))
                    return redirect(url_for(
                        "admin.upcoming_view",
                        add_error=f'"{name}" was added, but the poster was not saved (allowed formats: {allowed}).',
                    ))
            logger.info("Upcoming title added: %r.", name)
            return redirect(url_for("admin.upcoming_view"))

        elif action == "delete":
            delete_upcoming(cfg.upcoming_path, request.form.get("id", ""))
            logger.info("Upcoming title deleted (id=%s).", request.form.get("id", ""))
            return redirect(url_for("admin.upcoming_view"))

        elif action == "announce":
            ids = request.form.getlist("ids")
            entries = get_many(cfg.upcoming_path, ids)
            announce_error = None
            sent = False

            full_settings = load_settings(cfg.settings_path)
            if not full_settings.upcoming_notifications_enabled:
                announce_error = "The \"Upcoming Content Notifications\" module is currently disabled."
            elif not full_settings.smtp_enabled:
                announce_error = "Mail sending is disabled - enable it on the \"Mail Server\" page (a successful connection test is required first)."
            elif not entries:
                announce_error = "Select at least one title before sending."
            else:
                settings = full_settings.scoped("upcoming")
                client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
                items = _upcoming_items_for_send(entries)
                try:
                    send_email(
                        items, cfg, client, settings,
                        smtp=full_settings.resolve_smtp(cfg), template_name="email_upcoming.html",
                    )
                    sent = True
                    logger.info("Upcoming announcement sent for %d title(s).", len(items))
                except Exception as exc:
                    logger.exception("Failed to send the upcoming titles announcement")
                    # Short message in the redirect URL (no session needed)
                    # - sufficient for a typical SMTP error.
                    announce_error = f"Failed to send: {exc}"[:300]

            redirect_args = {"sent": int(sent)}
            if announce_error:
                redirect_args["announce_error"] = announce_error
            return redirect(url_for("admin.upcoming_view", **redirect_args))

        elif form_type == "toggle":
            toggle_settings = load_settings(cfg.settings_path)
            toggle_settings.upcoming_notifications_enabled = request.form.get("enabled") == "1"
            save_settings(cfg.settings_path, toggle_settings)
            logger.info("Upcoming Content Notifications module %s.", "enabled" if toggle_settings.upcoming_notifications_enabled else "disabled")
            return redirect(url_for("admin.upcoming_view"))

        elif form_type == "simple":
            settings = load_settings(cfg.settings_path)
            _save_simple_texts("upcoming", settings, cfg)
            saved = "simple"
            logger.info("Upcoming template: texts/colors saved.")

        elif form_type == "raw":
            raw_source = request.form.get("raw_source", raw_source)
            valid, error = _save_raw_template(EMAIL_UPCOMING_TEMPLATE_PATH, raw_source)
            saved = "raw" if valid else None
            raw_error = None if valid else error
            (logger.info if valid else logger.warning)(
                "Upcoming template: raw HTML %s.", "saved" if valid else f"rejected ({error})"
            )

        elif form_type == "blocks":
            blocks_settings = load_settings(cfg.settings_path)
            valid, error = _save_blocks(
                "upcoming", blocks_settings, cfg, EMAIL_UPCOMING_TEMPLATE_PATH, request.form.get("blocks_json", "[]")
            )
            saved = "blocks" if valid else None
            raw_error = None if valid else error
            if valid:
                raw_source = EMAIL_UPCOMING_TEMPLATE_PATH.read_text(encoding="utf-8")
            (logger.info if valid else logger.warning)(
                "Upcoming template: visual layout %s.", "saved" if valid else f"rejected ({error})"
            )

    sent = request.args.get("sent") == "1"
    announce_error = request.args.get("announce_error")
    add_error = request.args.get("add_error")
    settings = load_settings(cfg.settings_path)
    return render_template(
        "admin/upcoming.html",
        active="upcoming",
        wide_layout=True,
        items=list_upcoming(cfg.upcoming_path),
        poster_url=_poster_url,
        sent=sent,
        announce_error=announce_error,
        add_error=add_error,
        settings=settings.scoped("upcoming"),
        raw_source=raw_source,
        saved=saved,
        raw_error=raw_error,
        blocks_json=resolve_blocks_json(settings.upcoming_email_blocks),
        block_types=BLOCK_TYPES,
        notifications_enabled=settings.upcoming_notifications_enabled,
        mail_enabled=settings.smtp_enabled,
    )


@admin_bp.route("/upcoming/live-preview", methods=["POST"])
def upcoming_live_preview():
    """The single instant preview for the Upcoming tab: if ids are checked
    in the list, previews THOSE real titles (with their uploaded poster, if
    any); otherwise, a generic example. Either way it reflects the
    text/colors/HTML as typed in the form, without saving anything."""
    cfg = _config()
    payload = request.get_json(silent=True) or {}
    settings = load_settings(cfg.settings_path).scoped("upcoming")

    for field in (
        "template_intro_single", "template_intro_multi", "template_footer",
        "color_bg", "color_card", "color_header", "color_accent", "color_button", "color_text", "color_muted",
    ):
        if payload.get(field):
            setattr(settings, field, payload[field])

    ids = payload.get("ids") or []
    entries = get_many(cfg.upcoming_path, ids) if ids else []
    items = _upcoming_items_for_preview(entries) if entries else FAKE_UPCOMING_PREVIEW_ITEMS

    raw_source = payload.get("raw_source")
    if payload.get("blocks") is not None:
        try:
            raw_source = compile_blocks_to_html(payload.get("blocks") or [])
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Block error: {exc}"})

    valid, error = (True, "") if raw_source is None else _validate_template_source(raw_source)
    if not valid:
        return jsonify({"ok": False, "error": error})

    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    logo_url = url_for("admin.assets", filename="jellyfin-logo.png")
    try:
        html_out = render_preview_html(
            items, client, settings, raw_source=raw_source, logo_url=logo_url, template_name="email_upcoming.html"
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Render error: {exc}"})
    return jsonify({"ok": True, "html": html_out})


# ---------------------------------------------------------------------------
# Jellyfin API console
# ---------------------------------------------------------------------------

@admin_bp.route("/api", methods=["GET", "POST"])
def api_console_view():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    result = None
    save_msg = None

    effective_url = settings.jellyfin_url_override or cfg.jellyfin_url
    effective_key = settings.jellyfin_api_key_override or cfg.jellyfin_api_key

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_connection":
            settings.jellyfin_url_override = request.form.get("jellyfin_url_override", "").strip()

            new_key = request.form.get("jellyfin_api_key_override", "").strip()
            if new_key:
                settings.jellyfin_api_key_override = new_key
            elif request.form.get("clear_api_key_override"):
                settings.jellyfin_api_key_override = ""
            # Otherwise (field left empty, as always since it's never
            # pre-filled, and the "clear" box unchecked): keep the already
            # saved key as-is - previously, saving just the URL would
            # silently clear the API key override (the same class of bug
            # already fixed for the SMTP password).

            save_settings(cfg.settings_path, settings)
            save_msg = "Connection saved."
            effective_url = settings.jellyfin_url_override or cfg.jellyfin_url
            effective_key = settings.jellyfin_api_key_override or cfg.jellyfin_api_key
            logger.info("Jellyfin Connector settings saved (url=%s).", effective_url)

        elif action == "run":
            method = request.form.get("method", "GET")
            path = request.form.get("path", "/System/Info")
            query_string = request.form.get("query_string", "")
            result = run_request(effective_url, effective_key, method, path, query_string)

    return render_template(
        "admin/api_console.html",
        active="api_console",
        effective_url=effective_url,
        effective_key=effective_key,
        url_override_value=settings.jellyfin_url_override,
        using_override=bool(settings.jellyfin_url_override or settings.jellyfin_api_key_override),
        has_api_key_override=bool(settings.jellyfin_api_key_override),
        result=result,
        save_msg=save_msg,
        method=request.form.get("method", "GET"),
        path=request.form.get("path", "/System/Info"),
        query_string=request.form.get("query_string", ""),
    )


@admin_bp.route("/api/test", methods=["POST"])
def api_console_test():
    """Tests the connection (GET /System/Info) WITHOUT saving anything -
    reflects what's currently typed in the form (even if not saved yet),
    falls back to the already-saved override then to .env if the fields
    are left empty. JSON response displayed directly on the page, without
    a reload."""
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    payload = request.get_json(silent=True) or {}

    url_override = (payload.get("jellyfin_url_override") or "").strip()
    key_override = (payload.get("jellyfin_api_key_override") or "").strip()
    effective_url = url_override or settings.jellyfin_url_override or cfg.jellyfin_url
    effective_key = key_override or settings.jellyfin_api_key_override or cfg.jellyfin_api_key

    result = run_request(effective_url, effective_key, "GET", "/System/Info", "")
    return jsonify(result)


# ---------------------------------------------------------------------------
# Mail server (SMTP) - host/port/encryption, credentials, sender,
# recipients. Configurable from the admin, without restarting the service.
# Any standard SMTP provider is supported.
# ---------------------------------------------------------------------------

def _smtp_fingerprint(smtp) -> str:
    """Fingerprint of the effective SMTP config (host/port/credentials/
    sender/recipients) - used to know whether the current config is
    exactly the one that was validated by a successful connection test,
    without storing the password in plaintext anywhere else than
    settings.json (where it already is)."""
    raw = "|".join([
        smtp.host, str(smtp.port), smtp.encryption, smtp.username, smtp.password,
        smtp.sender_name, smtp.sender_email, ",".join(smtp.recipients),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


@admin_bp.route("/mail-server", methods=["GET", "POST"])
def mail_server_view():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    saved = False
    test_result = None
    toggle_error = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save":
            settings.smtp_host_override = request.form.get("smtp_host_override", "").strip()
            settings.smtp_port_override = request.form.get("smtp_port_override", type=int) or 0
            encryption = request.form.get("smtp_encryption_override", "").strip().lower()
            settings.smtp_encryption_override = encryption if encryption in SMTP_ENCRYPTIONS else ""
            settings.smtp_username_override = request.form.get("smtp_username_override", "").strip()

            new_password = request.form.get("smtp_password_override", "")
            if new_password:
                settings.smtp_password_override = new_password
            elif request.form.get("clear_password_override"):
                settings.smtp_password_override = ""
            # Otherwise (field left empty, "clear" box unchecked): keep the
            # already saved password as-is - we don't want saving some
            # other field to wipe the password just because the form never
            # displays it back in plaintext.

            settings.sender_name_override = request.form.get("sender_name_override", "").strip()
            settings.sender_email_override = request.form.get("sender_email_override", "").strip()
            settings.recipients_override = request.form.get("recipients_override", "").strip()

            # If the saved fields differ from the last successfully tested
            # config, sending is turned off automatically: we never want to
            # leave never-verified credentials (or ones modified since
            # their validation) silently enabled.
            new_fingerprint = _smtp_fingerprint(settings.resolve_smtp(cfg))
            if new_fingerprint != settings.smtp_validated_fingerprint:
                settings.smtp_enabled = False
            save_settings(cfg.settings_path, settings)
            saved = True
            logger.info("Mail server: connection settings saved (host=%s).", settings.smtp_host_override or cfg.smtp_host)

        elif action == "test":
            smtp = settings.resolve_smtp(cfg)
            test_recipient = request.form.get("test_recipient", "").strip() or (smtp.recipients[0] if smtp.recipients else "")
            if not test_recipient:
                test_result = {"ok": False, "error": "No recipient configured to send the test to."}
            else:
                try:
                    send_test_email(smtp, test_recipient, history_path=cfg.mail_history_path)
                    test_result = {"ok": True, "recipient": test_recipient}
                    # The currently SAVED config has just proven that it
                    # works -> it becomes eligible for enabling the
                    # send switch.
                    settings.smtp_validated_fingerprint = _smtp_fingerprint(smtp)
                    save_settings(cfg.settings_path, settings)
                    logger.info("Mail server: test mail sent successfully to %s, configuration validated.", test_recipient)
                except Exception as exc:
                    logger.exception("Failed to send the test mail")
                    test_result = {"ok": False, "error": str(exc)}

        elif action == "toggle":
            current_fp = _smtp_fingerprint(settings.resolve_smtp(cfg))
            is_validated = bool(settings.smtp_validated_fingerprint) and settings.smtp_validated_fingerprint == current_fp
            want_enabled = request.form.get("enabled") == "1"
            if want_enabled and not is_validated:
                toggle_error = "Run a successful connection test with the saved settings before enabling mail sending."
            else:
                settings.smtp_enabled = want_enabled
                save_settings(cfg.settings_path, settings)
                logger.info("Mail server: mail sending %s.", "enabled" if want_enabled else "disabled")

    smtp = settings.resolve_smtp(cfg)
    is_validated = bool(settings.smtp_validated_fingerprint) and settings.smtp_validated_fingerprint == _smtp_fingerprint(smtp)
    return render_template(
        "admin/mail_server.html",
        active="mail_server",
        settings=settings,
        smtp=smtp,
        default_host=cfg.smtp_host,
        default_port=cfg.smtp_port,
        default_encryption=cfg.smtp_encryption,
        default_username=cfg.smtp_username,
        default_sender_name=cfg.sender_name,
        default_sender_email=cfg.sender_email,
        default_recipients=", ".join(cfg.recipients),
        has_password_override=bool(settings.smtp_password_override),
        encryptions=SMTP_ENCRYPTIONS,
        saved=saved,
        test_result=test_result,
        toggle_error=toggle_error,
        is_validated=is_validated,
        mail_active=settings.smtp_enabled and is_validated,
    )


# ---------------------------------------------------------------------------
# History of sent mails (notifications, announcements, tests) - a real
# browsable log rather than just the last send's status.
# ---------------------------------------------------------------------------

@admin_bp.route("/mail-history")
def mail_history_view():
    cfg = _config()
    limit = request.args.get("limit", default=200, type=int)
    return render_template(
        "admin/mail_history.html",
        active="mail_history",
        history=mail_history.list_history(cfg.mail_history_path, limit),
        limit=limit,
    )


# ---------------------------------------------------------------------------
# "Health" page - reflects the public /health endpoint (no authentication,
# meant for an external monitoring system like Uptime Kuma), with the
# direct link to copy included.
# ---------------------------------------------------------------------------

@admin_bp.route("/monitoring")
def health_view():
    poller = _poller()
    poll_status = poller.status() if poller else None
    healthy = bool(poll_status["healthy"]) if poll_status else True
    return render_template(
        "admin/health.html",
        active="health",
        poll_status=poll_status,
        healthy=healthy,
        health_url=url_for("webhook.health", _external=True),
        metrics_url=url_for("webhook.metrics_endpoint", _external=True),
    )


# ---------------------------------------------------------------------------
# Export / import of settings.json (one-click backup/restore) - does NOT
# touch the data (the pending queue, "upcoming" titles + posters, mail
# history, the poller's "already seen" state) or the HTML templates.
# ---------------------------------------------------------------------------

@admin_bp.route("/settings/export")
def settings_export():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    payload = json.dumps(settings.to_dict(), indent=2, ensure_ascii=False)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger.info("Settings exported.")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="jellyfin-notifier-settings-{stamp}.json"'},
    )


@admin_bp.route("/settings/import", methods=["POST"])
def settings_import():
    # Redirects to setup.setup_view (the "Configuration" page), not the
    # dashboard - the settings.json backup/restore/reset cards moved there
    # to sit next to .env's own import/reset, since the admin thinks of
    # both as "the configuration" even though they're two separate files.
    cfg = _config()
    upload = request.files.get("settings_file")
    if not upload or not upload.filename:
        return redirect(url_for("setup.setup_view", import_error="Choose a settings JSON file first."))
    try:
        data = json.loads(upload.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        imported = Settings.from_dict(data)
    except Exception as exc:
        logger.warning("Settings import rejected: %s", exc)
        return redirect(url_for("setup.setup_view", import_error=f"Invalid settings file: {exc}"))
    save_settings(cfg.settings_path, imported)
    logger.warning("Settings imported from %r, overwriting the current configuration.", upload.filename)
    return redirect(url_for("setup.setup_view", imported=1))
