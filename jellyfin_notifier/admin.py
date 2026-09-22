"""Interface d'admin web (/admin) : planning des notifications, éditeur de
template "CMS", aperçu du prochain mail, annonces de titres à venir, console
de requêtes API Jellyfin, et gestion du service (start/stop/restart + logs).

Protégée par HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD dans .env) -
pensée pour un accès LAN uniquement (pas exposée sur internet)."""

from __future__ import annotations

import hmac
import logging
import shutil
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, url_for
from jinja2 import Environment as JinjaEnv
from jinja2 import TemplateSyntaxError

from . import service_control
from .api_console import run_request
from .email_sender import TEMPLATES_DIR, _prepare_content, send_email
from .jellyfin_client import JellyfinClient
from .pending import load_pending
from .schedule import WEEKDAY_NAMES_FR, is_within_window, next_allowed_datetime
from .settings import Settings, load_settings, save_settings
from .upcoming import add_upcoming, delete_upcoming, get_many, item_from_upcoming, list_upcoming

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)

EMAIL_TEMPLATE_PATH = TEMPLATES_DIR / "email.html"

FAKE_PREVIEW_ITEMS = [
    {
        "item_id": None,
        "item_type": "Movie",
        "name": "Terminator",
        "year": 1984,
        "overview": (
            "À Los Angeles en 1984, un Terminator, cyborg surgi du futur, a pour "
            "mission d'exécuter Sarah Connor, une jeune femme dont l'enfant à "
            "naître doit sauver l'humanité."
        ),
        "genres": ["Action", "Science-Fiction"],
        "community_rating": 7.5,
        "run_time_ticks": 63_000_000_000,
        "type_label": "Film",
    },
]


def _config():
    return current_app.config["JF_CONFIG"]


def _poller():
    return current_app.config.get("JF_POLLER")


@admin_bp.before_request
def _require_auth():
    cfg = _config()
    auth = request.authorization
    if not auth or not (
        hmac.compare_digest(auth.username or "", cfg.admin_username)
        and hmac.compare_digest(auth.password or "", cfg.admin_password)
    ):
        return Response(
            "Authentification requise.",
            401,
            {"WWW-Authenticate": 'Basic realm="Jellyfin Notifier Admin"'},
        )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@admin_bp.route("/")
def dashboard():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    poller = _poller()
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
    )


# ---------------------------------------------------------------------------
# Gestion du service (start/stop/restart) + logs
# ---------------------------------------------------------------------------

@admin_bp.route("/service/<action>", methods=["POST"])
def service_action_route(action: str):
    ok, output = service_control.service_action(action)
    return jsonify({"ok": ok, "output": output, "status": service_control.get_status()})


@admin_bp.route("/logs")
def logs():
    lines = request.args.get("lines", default=300, type=int)
    return render_template("admin/logs.html", active="logs", logs_text=service_control.get_logs(lines), lines=lines)


@admin_bp.route("/logs/data")
def logs_data():
    lines = request.args.get("lines", default=300, type=int)
    return jsonify({"logs": service_control.get_logs(lines), "service": service_control.get_status()})


# ---------------------------------------------------------------------------
# Planning (jours ouvrés / heures autorisées)
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

    return render_template(
        "admin/schedule.html",
        active="schedule",
        settings=settings,
        weekday_names=list(enumerate(WEEKDAY_NAMES_FR)),
        saved=saved,
        within_window=is_within_window(settings),
        next_slot=next_allowed_datetime(settings),
    )


# ---------------------------------------------------------------------------
# Éditeur de template (champs simples + éditeur HTML brut avec validation)
# ---------------------------------------------------------------------------

def _validate_template_source(source: str) -> tuple[bool, str]:
    env = JinjaEnv(autoescape=True)
    try:
        template = env.from_string(source)
        template.render(
            items=[
                {
                    "name": "Exemple", "year": 2024, "overview": "Synopsis d'exemple.",
                    "type_label": "Film", "genres": "Action", "rating": "7.5",
                    "duration": "1h47", "image_cid": None, "deep_link": "#",
                }
            ],
            count=1,
            date="01/01/2026 12:00",
            logo_src=None,
            intro_text="Un nouveau contenu est disponible !",
            footer_text="Envoyé automatiquement par ton serveur Jellyfin",
        )
    except TemplateSyntaxError as exc:
        return False, f"Erreur de syntaxe ligne {exc.lineno} : {exc.message}"
    except Exception as exc:
        return False, f"Erreur au rendu : {exc}"
    return True, ""


@admin_bp.route("/template/validate", methods=["POST"])
def template_validate():
    source = (request.get_json(silent=True) or {}).get("source", "")
    valid, error = _validate_template_source(source)
    return jsonify({"valid": valid, "error": error})


@admin_bp.route("/template", methods=["GET", "POST"])
def template_view():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    saved = None
    raw_error = None
    raw_source = EMAIL_TEMPLATE_PATH.read_text(encoding="utf-8")

    if request.method == "POST":
        form_type = request.form.get("form_type")

        if form_type == "simple":
            settings.template_subject_single = request.form.get("template_subject_single", settings.template_subject_single)
            settings.template_subject_multi = request.form.get("template_subject_multi", settings.template_subject_multi)
            settings.template_intro_single = request.form.get("template_intro_single", settings.template_intro_single)
            settings.template_intro_multi = request.form.get("template_intro_multi", settings.template_intro_multi)
            settings.template_footer = request.form.get("template_footer", settings.template_footer)
            save_settings(cfg.settings_path, settings)
            saved = "simple"

        elif form_type == "raw":
            raw_source = request.form.get("raw_source", raw_source)
            valid, error = _validate_template_source(raw_source)
            if valid:
                backup_dir = EMAIL_TEMPLATE_PATH.parent / "backups"
                backup_dir.mkdir(exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(EMAIL_TEMPLATE_PATH, backup_dir / f"email.{stamp}.html.bak")
                EMAIL_TEMPLATE_PATH.write_text(raw_source, encoding="utf-8")
                saved = "raw"
            else:
                raw_error = error

    return render_template(
        "admin/template.html",
        active="template",
        settings=settings,
        raw_source=raw_source,
        saved=saved,
        raw_error=raw_error,
    )


# ---------------------------------------------------------------------------
# Aperçu du prochain mail
# ---------------------------------------------------------------------------

@admin_bp.route("/preview")
def preview():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    pending = load_pending(cfg.pending_items_path)
    items = pending if pending else FAKE_PREVIEW_ITEMS
    is_fake = not pending

    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    html, _ = _prepare_content(items, client, settings)
    return render_template("admin/preview.html", active="preview", is_fake=is_fake, item_count=len(items), preview_html=html)


@admin_bp.route("/preview/frame")
def preview_frame():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    pending = load_pending(cfg.pending_items_path)
    items = pending if pending else FAKE_PREVIEW_ITEMS
    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    html, _ = _prepare_content(items, client, settings)
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------------
# Titres "à venir" (annonces manuelles)
# ---------------------------------------------------------------------------

@admin_bp.route("/upcoming", methods=["GET", "POST"])
def upcoming_view():
    cfg = _config()
    sent = False

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                add_upcoming(
                    cfg.upcoming_path,
                    name=name,
                    year=request.form.get("year", "").strip(),
                    note=request.form.get("note", "").strip(),
                    type_label=request.form.get("type_label", "Film"),
                )

        elif action == "delete":
            delete_upcoming(cfg.upcoming_path, request.form.get("id", ""))

        elif action == "announce":
            ids = request.form.getlist("ids")
            entries = get_many(cfg.upcoming_path, ids)
            if entries:
                settings = load_settings(cfg.settings_path)
                client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
                items = [item_from_upcoming(e) for e in entries]
                try:
                    send_email(items, cfg, client, settings)
                    sent = True
                except Exception:
                    logger.exception("Échec d'envoi de l'annonce des titres à venir")

        return redirect(url_for("admin.upcoming_view", sent=int(sent)))

    sent = request.args.get("sent") == "1"
    return render_template("admin/upcoming.html", active="upcoming", items=list_upcoming(cfg.upcoming_path), sent=sent)


# ---------------------------------------------------------------------------
# Console API Jellyfin
# ---------------------------------------------------------------------------

@admin_bp.route("/api-console", methods=["GET", "POST"])
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
            settings.jellyfin_api_key_override = request.form.get("jellyfin_api_key_override", "").strip()
            save_settings(cfg.settings_path, settings)
            save_msg = "Connexion enregistrée."
            effective_url = settings.jellyfin_url_override or cfg.jellyfin_url
            effective_key = settings.jellyfin_api_key_override or cfg.jellyfin_api_key

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
        result=result,
        save_msg=save_msg,
        method=request.form.get("method", "GET"),
        path=request.form.get("path", "/System/Info"),
        query_string=request.form.get("query_string", ""),
    )
