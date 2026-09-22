"""Interface d'admin web (/admin) : planning des notifications, éditeur de
template "CMS", aperçu du prochain mail, annonces de titres à venir, console
de requêtes API Jellyfin, et gestion du service (start/stop/restart + logs).

Protégée par HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD dans .env) -
pensée pour un accès LAN uniquement (pas exposée sur internet)."""

from __future__ import annotations

import hmac
import html.parser
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from jinja2 import Environment as JinjaEnv
from jinja2 import TemplateSyntaxError

from . import service_control
from .api_console import run_request
from .email_sender import TEMPLATES_DIR, render_preview_html, send_email
from .jellyfin_client import JellyfinClient
from .pending import load_pending
from .schedule import WEEKDAY_NAMES_EN, is_within_window, next_allowed_datetime
from .settings import Settings, load_settings, save_settings
from .upcoming import add_upcoming, delete_upcoming, get_many, item_from_upcoming, list_upcoming

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)

EMAIL_TEMPLATE_PATH = TEMPLATES_DIR / "email.html"
ASSETS_DIR = Path(__file__).parent / "assets"

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
        "_fake_deep_link": "#preview",
    },
]

# Item d'exemple distinct pour l'aperçu de l'onglet "À venir" (tant qu'aucun
# titre n'est encore ajouté à la liste) - volontairement différent de
# FAKE_PREVIEW_ITEMS pour que les deux aperçus ne se ressemblent pas.
FAKE_UPCOMING_PREVIEW_ITEMS = [
    {
        "item_id": None,
        "item_type": "Movie",
        "name": "Dune: Part Three",
        "year": None,
        "overview": "Bientôt disponible sur Jellyfin.",
        "genres": None,
        "community_rating": None,
        "run_time_ticks": None,
        "type_label": "Bientôt • Film",
    },
]


def _config():
    return current_app.config["JF_CONFIG"]


def _poller():
    return current_app.config.get("JF_POLLER")


# ---------------------------------------------------------------------------
# Fichiers statiques (logo affiché dans le header de l'admin)
# ---------------------------------------------------------------------------

@admin_bp.route("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(ASSETS_DIR, filename)


# ---------------------------------------------------------------------------
# Validation HTML : la validation Jinja seule (compile + rend le template)
# ne détecte AUCUN problème de balisage HTML - un attribut mal formé du
# genre `<html =d1d1>` est du Jinja/texte parfaitement valide, donc "compile"
# sans erreur. On ajoute donc une vérification structurelle légère (balises
# non fermées / mal imbriquées, attributs mal formés) en plus du check Jinja.
# ---------------------------------------------------------------------------

_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Attribut valide : nom (lettres/chiffres/-/:/_ , doit commencer par une
# lettre) suivi optionnellement de ="valeur"/'valeur'/valeur. Un attribut du
# genre `=d1d1` (nom vide, commence par "=") ne matche pas -> signalé.
_ATTR_RE = re.compile(
    r'\s+([a-zA-Z][a-zA-Z0-9\-:_]*)(\s*=\s*("[^"]*"|\'[^\']*\'|[^\s"\'=<>`]+))?'
)


def _check_html_structure(source: str) -> str | None:
    """Retourne un message d'erreur (ou None si rien détecté). Ignore les
    blocs Jinja ({% ... %}, {{ ... }}) pour ne pas les confondre avec du HTML."""
    # Neutralise les blocs Jinja pour ne pas perturber le parseur HTML
    # (ex: {% for item in items %} contient des `<`/`>` implicites via le texte
    # généré, mais surtout on ne veut pas que le tag-balancer s'embrouille).
    cleaned = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", "", source, flags=re.DOTALL)

    errors: list[str] = []
    stack: list[tuple[str, int]] = []

    class _Checker(html.parser.HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in _VOID_ELEMENTS:
                stack.append((tag, self.getpos()[0]))

        def handle_startendtag(self, tag, attrs):
            pass  # auto-fermée (<br />) : rien à empiler

        def handle_endtag(self, tag):
            if tag in _VOID_ELEMENTS:
                return
            if not stack:
                errors.append(f"line {self.getpos()[0]}: closing tag </{tag}> has no matching opening tag")
                return
            # Cherche la balise ouvrante correspondante dans la pile (gère les
            # balises mal imbriquées comme <a><b></a></b>)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    unclosed = stack[i + 1:]
                    if unclosed:
                        names = ", ".join(f"<{n}>" for n, _ in unclosed)
                        errors.append(f"line {self.getpos()[0]}: {names} not closed before </{tag}>")
                    del stack[i:]
                    return
            errors.append(f"line {self.getpos()[0]}: </{tag}> does not match any open tag")

        def error(self, message):  # requis par certaines versions de html.parser
            errors.append(message)

    parser = _Checker(convert_charrefs=True)
    try:
        parser.feed(cleaned)
        parser.close()
    except Exception as exc:  # pragma: no cover - garde-fou
        return f"HTML parsing error: {exc}"

    if stack:
        names = ", ".join(f"<{n}> (line {ln})" for n, ln in stack)
        errors.append(f"unclosed tag(s): {names}")

    # Vérifie les attributs mal formés dans chaque balise ouvrante (ex: <html =d1d1>)
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
            # Sécurité minimale : n'autorise que les redirections internes (évite un open redirect).
            if not next_url.startswith("/"):
                next_url = url_for("admin.dashboard")
            return redirect(next_url)
        error = "Invalid username or password."

    return render_template("admin/login.html", error=error, next=request.args.get("next", ""))


@admin_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


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
        weekday_names=list(enumerate(WEEKDAY_NAMES_EN)),
        saved=saved,
        within_window=is_within_window(settings),
        next_slot=next_allowed_datetime(settings),
    )


# ---------------------------------------------------------------------------
# Éditeur de template (champs simples + éditeur HTML brut avec validation)
# ---------------------------------------------------------------------------

def _validate_template_source(source: str) -> tuple[bool, str]:
    env = JinjaEnv(autoescape=True)
    settings = Settings()
    try:
        template = env.from_string(source)
        template.render(
            items=[
                {
                    "name": "Exemple", "year": 2024, "overview": "Synopsis d'exemple.",
                    "type_label": "Film", "genres": "Action", "rating": "7.5",
                    "duration": "1h47", "image_cid": None, "image_url": None, "deep_link": "#",
                }
            ],
            count=1,
            date="01/01/2026 12:00",
            logo_src=None,
            intro_text="Un nouveau contenu est disponible !",
            footer_text="Envoyé automatiquement par ton serveur Jellyfin",
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
            for field in ("color_bg", "color_card", "color_header", "color_accent", "color_button", "color_text", "color_muted"):
                value = request.form.get(field)
                if value:
                    setattr(settings, field, value)
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
        wide_layout=True,
        settings=settings,
        raw_source=raw_source,
        saved=saved,
        raw_error=raw_error,
    )


@admin_bp.route("/template/live-preview", methods=["POST"])
def template_live_preview():
    """Aperçu instantané (rien n'est sauvegardé) pour le panneau de droite de
    l'éditeur : reflète le HTML brut ET les champs simples tels qu'ils sont
    actuellement tapés dans le formulaire, pas la version sur disque."""
    cfg = _config()
    payload = request.get_json(silent=True) or {}
    settings = load_settings(cfg.settings_path)

    for field in (
        "template_intro_single", "template_intro_multi", "template_footer",
        "color_bg", "color_card", "color_header", "color_accent", "color_button", "color_text", "color_muted",
    ):
        if payload.get(field):
            setattr(settings, field, payload[field])

    raw_source = payload.get("raw_source")
    valid, error = (True, "") if raw_source is None else _validate_template_source(raw_source)
    if not valid:
        return jsonify({"ok": False, "error": error})

    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    logo_url = url_for("admin.assets", filename="jellyfin-logo.png")
    try:
        html_out = render_preview_html(FAKE_PREVIEW_ITEMS, client, settings, raw_source=raw_source, logo_url=logo_url)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Render error: {exc}"})
    return jsonify({"ok": True, "html": html_out})


# ---------------------------------------------------------------------------
# Aperçu du prochain mail
# ---------------------------------------------------------------------------

@admin_bp.route("/preview")
def preview():
    # L'aperçu vit désormais directement dans l'onglet "Notifications ajout
    # d'items" (édition + aperçu côte à côte) - on redirige l'ancienne URL.
    return redirect(url_for("admin.template_view"))


@admin_bp.route("/preview/frame")
def preview_frame():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    pending = load_pending(cfg.pending_items_path)
    items = pending if pending else FAKE_PREVIEW_ITEMS
    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    logo_url = url_for("admin.assets", filename="jellyfin-logo.png")
    html_out = render_preview_html(items, client, settings, logo_url=logo_url)
    return Response(html_out, mimetype="text/html")


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
    return render_template(
        "admin/upcoming.html",
        active="upcoming",
        wide_layout=True,
        items=list_upcoming(cfg.upcoming_path),
        sent=sent,
    )


@admin_bp.route("/upcoming/preview")
def upcoming_preview():
    """Aperçu de l'annonce "Bientôt disponible" pour les titres actuellement
    cochés dans l'onglet À venir (ids passés en query string, mis à jour en
    JS à chaque changement de case) - un exemple si rien n'est coché."""
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    ids = request.args.getlist("ids")
    entries = get_many(cfg.upcoming_path, ids) if ids else []
    items = [item_from_upcoming(e) for e in entries] if entries else FAKE_UPCOMING_PREVIEW_ITEMS

    client = JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_public_url)
    logo_url = url_for("admin.assets", filename="jellyfin-logo.png")
    html_out = render_preview_html(items, client, settings, logo_url=logo_url)
    return Response(html_out, mimetype="text/html")


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
            save_msg = "Connection saved."
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
