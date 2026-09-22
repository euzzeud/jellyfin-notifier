"""Interface d'admin web (/admin) : planning des notifications, éditeur de
template "CMS", aperçu du prochain mail, annonces de titres à venir, console
de requêtes API Jellyfin, et gestion du service (start/stop/restart + logs).

Protégée par HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD dans .env) -
pensée pour un accès LAN uniquement (pas exposée sur internet)."""

from __future__ import annotations

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

from . import service_control
from .api_console import run_request
from .config import SMTP_ENCRYPTIONS
from .email_blocks import BLOCK_TYPES, compile_blocks_to_html
from .email_sender import TEMPLATES_DIR, render_preview_html, send_email, send_test_email
from .jellyfin_client import JellyfinClient
from .pending import load_pending
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


@admin_bp.context_processor
def _inject_header_context():
    """Rend l'URL Jellyfin (.env) disponible dans TOUS les templates admin -
    affichée en haut à droite du header. Avant, le template lisait
    `config.jellyfin_url` (la config Flask elle-même, pas notre Config à
    nous) : ça ne levait pas d'erreur grâce au rendu "silencieux" de Jinja
    sur une valeur indéfinie, mais n'affichait jamais rien non plus.
    Volontairement `cfg.jellyfin_url` (déjà en mémoire, .env) plutôt que la
    surcharge éventuelle de Settings : ce contexte tourne sur CHAQUE rendu de
    template, donc pas question d'ajouter une lecture+parsing JSON de plus
    par page juste pour un affichage cosmétique - les pages qui ont
    réellement besoin de la valeur effective (avec surcharge) la calculent
    déjà elles-mêmes (cf. mail_server_view, api_console_view)."""
    try:
        return {"jellyfin_url": _config().jellyfin_url}
    except (RuntimeError, KeyError):
        return {}


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

    poll_status = poller.status() if poller else None
    service_status = service_control.get_status()
    pending = load_pending(cfg.pending_items_path)
    logs_tail = service_control.get_logs(40)
    settings_reset = request.args.get("settings_reset") == "1"

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
        settings_reset=settings_reset,
        default_item_types=",".join(sorted(cfg.notify_item_types)),
        default_interval=cfg.poll_interval_seconds,
    )


# ---------------------------------------------------------------------------
# Réinitialisation des paramètres (settings.json) aux valeurs par défaut du
# code - texte/couleurs des mails, planning, surcharges poller/Jellyfin/SMTP.
# Ne touche PAS aux données (file d'attente, titres "à venir" + affiches,
# état "déjà vu" du poller) ni aux templates HTML bruts édités (email.html /
# email_upcoming.html, qui ont leurs propres sauvegardes dans templates/backups/).
# ---------------------------------------------------------------------------

@admin_bp.route("/settings/reset", methods=["POST"])
def settings_reset():
    cfg = _config()
    save_settings(cfg.settings_path, Settings())
    return redirect(url_for("admin.dashboard", settings_reset=1))


# ---------------------------------------------------------------------------
# Contrôle du poller (start/stop/pause/resume) depuis l'admin
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

    return jsonify({"ok": True, "status": poller.status()})


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


def _save_simple_texts(scope: str, settings: Settings, cfg) -> None:
    prefix = _FIELD_PREFIX[scope]
    for field in _SIMPLE_FIELDS:
        value = request.form.get(field)
        if value:
            setattr(settings, prefix + field, value)
    save_settings(cfg.settings_path, settings)


def _save_raw_template(template_path: Path, raw_source: str) -> tuple[bool, str]:
    """Valide puis sauvegarde un template HTML brut (avec backup horodaté).
    Retourne (ok, error)."""
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
    """Compile les blocs de l'éditeur visuel en HTML/Jinja2 puis réutilise
    exactement le même chemin de sauvegarde (validation + backup horodaté)
    que l'éditeur HTML brut - email_sender.py n'a besoin d'aucun changement."""
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
    """Vue générique de l'éditeur (textes + couleurs + HTML brut) pour "New
    Content Notifications" (scope="new") - chacun a ses propres
    textes/couleurs (Settings.scoped) et son propre fichier de template."""
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
            return redirect(url_for("admin.template_view"))

        elif form_type == "simple":
            _save_simple_texts(scope, settings, cfg)
            saved = "simple"

        elif form_type == "raw":
            raw_source = request.form.get("raw_source", raw_source)
            valid, error = _save_raw_template(template_path, raw_source)
            saved = "raw" if valid else None
            raw_error = None if valid else error

        elif form_type == "blocks":
            valid, error = _save_blocks(scope, settings, cfg, template_path, request.form.get("blocks_json", "[]"))
            saved = "blocks" if valid else None
            raw_error = None if valid else error
            if valid:
                raw_source = template_path.read_text(encoding="utf-8")

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
        blocks_json=getattr(settings, f"{prefix}email_blocks", "[]") or "[]",
        block_types=BLOCK_TYPES,
        notifications_enabled=settings.new_notifications_enabled,
    )


def _template_live_preview(scope: str, template_name: str, fake_items: list[dict]):
    """Aperçu instantané (rien n'est sauvegardé) : reflète le HTML brut ET
    les champs simples tels qu'ils sont actuellement tapés dans le
    formulaire, pas la version sur disque."""
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


@admin_bp.route("/template", methods=["GET", "POST"])
def template_view():
    return _template_editor_view("new", EMAIL_TEMPLATE_PATH, active="template")


@admin_bp.route("/template/live-preview", methods=["POST"])
def template_live_preview():
    return _template_live_preview("new", "email.html", FAKE_PREVIEW_ITEMS)


# ---------------------------------------------------------------------------
# Aperçu du prochain mail
# ---------------------------------------------------------------------------

@admin_bp.route("/preview")
def preview():
    # L'aperçu vit désormais directement dans l'onglet "New Content
    # Notifications" (édition + aperçu côte à côte) - on redirige l'ancienne URL.
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
# Titres "à venir" (annonces manuelles) - même éditeur (textes/couleurs/HTML
# brut + aperçu live) que "New Content Notifications", mais scope="upcoming"
# et avec en plus la gestion des affiches uploadées manuellement.
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
    """item_from_upcoming + injecte le chemin disque de l'affiche uploadée
    (utilisée par email_sender pour l'attacher en cid: dans le vrai mail)."""
    items = []
    for e in entries:
        item = item_from_upcoming(e)
        item["_local_poster_path"] = _poster_disk_path(e)
        items.append(item)
    return items


def _upcoming_items_for_preview(entries: list[dict]) -> list[dict]:
    """item_from_upcoming + injecte l'URL servie de l'affiche uploadée
    (utilisée pour l'aperçu navigateur)."""
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
                type_label=request.form.get("type_label", "Film"),
            )
            poster = request.files.get("poster")
            if poster and poster.filename:
                stored = save_poster(cfg.upcoming_path, entry["id"], poster.filename, poster.read())
                if stored is None:
                    # save_poster() échoue silencieusement (extension non
                    # autorisée) - avant, rien ne le signalait à l'utilisateur,
                    # qui voyait juste son titre ajouté SANS l'affiche qu'il
                    # venait d'uploader, sans savoir pourquoi.
                    allowed = ", ".join(sorted(ALLOWED_POSTER_EXTENSIONS))
                    return redirect(url_for(
                        "admin.upcoming_view",
                        add_error=f'"{name}" was added, but the poster was not saved (allowed formats: {allowed}).',
                    ))
            return redirect(url_for("admin.upcoming_view"))

        elif action == "delete":
            delete_upcoming(cfg.upcoming_path, request.form.get("id", ""))
            return redirect(url_for("admin.upcoming_view"))

        elif action == "announce":
            ids = request.form.getlist("ids")
            entries = get_many(cfg.upcoming_path, ids)
            announce_error = None
            sent = False

            full_settings = load_settings(cfg.settings_path)
            if not full_settings.upcoming_notifications_enabled:
                announce_error = "The \"Upcoming Content Notifications\" module is currently disabled."
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
                except Exception as exc:
                    logger.exception("Échec d'envoi de l'annonce des titres à venir")
                    # Message court dans l'URL de redirection (pas de session
                    # nécessaire) - suffisant pour une erreur SMTP typique.
                    announce_error = f"Failed to send: {exc}"[:300]

            redirect_args = {"sent": int(sent)}
            if announce_error:
                redirect_args["announce_error"] = announce_error
            return redirect(url_for("admin.upcoming_view", **redirect_args))

        elif form_type == "toggle":
            toggle_settings = load_settings(cfg.settings_path)
            toggle_settings.upcoming_notifications_enabled = request.form.get("enabled") == "1"
            save_settings(cfg.settings_path, toggle_settings)
            return redirect(url_for("admin.upcoming_view"))

        elif form_type == "simple":
            settings = load_settings(cfg.settings_path)
            _save_simple_texts("upcoming", settings, cfg)
            saved = "simple"

        elif form_type == "raw":
            raw_source = request.form.get("raw_source", raw_source)
            valid, error = _save_raw_template(EMAIL_UPCOMING_TEMPLATE_PATH, raw_source)
            saved = "raw" if valid else None
            raw_error = None if valid else error

        elif form_type == "blocks":
            blocks_settings = load_settings(cfg.settings_path)
            valid, error = _save_blocks(
                "upcoming", blocks_settings, cfg, EMAIL_UPCOMING_TEMPLATE_PATH, request.form.get("blocks_json", "[]")
            )
            saved = "blocks" if valid else None
            raw_error = None if valid else error
            if valid:
                raw_source = EMAIL_UPCOMING_TEMPLATE_PATH.read_text(encoding="utf-8")

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
        blocks_json=settings.upcoming_email_blocks or "[]",
        block_types=BLOCK_TYPES,
        notifications_enabled=settings.upcoming_notifications_enabled,
    )


@admin_bp.route("/upcoming/live-preview", methods=["POST"])
def upcoming_live_preview():
    """Aperçu instantané unique pour l'onglet Upcoming : si des ids sont
    cochés dans la liste, prévisualise CES titres réels (avec leur affiche
    uploadée s'il y en a une) ; sinon, un exemple générique. Dans tous les
    cas reflète les textes/couleurs/HTML tels que tapés dans le formulaire,
    sans rien sauvegarder."""
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
# Console API Jellyfin
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
            # Sinon (champ laissé vide, comme toujours puisqu'il n'est jamais
            # pré-rempli, et case "effacer" pas cochée) : on garde la clé déjà
            # enregistrée telle quelle - avant, sauvegarder juste l'URL
            # effaçait silencieusement la clé API en surcharge (même bug de
            # classe que celui déjà corrigé pour le mot de passe SMTP).

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
        has_api_key_override=bool(settings.jellyfin_api_key_override),
        result=result,
        save_msg=save_msg,
        method=request.form.get("method", "GET"),
        path=request.form.get("path", "/System/Info"),
        query_string=request.form.get("query_string", ""),
    )


@admin_bp.route("/api/test", methods=["POST"])
def api_console_test():
    """Teste la connexion (GET /System/Info) SANS rien sauvegarder - reflète
    ce qui est actuellement tapé dans le formulaire (même si pas encore
    enregistré), retombe sur la surcharge déjà sauvegardée puis sur .env si
    les champs sont laissés vides. Réponse JSON affichée directement dans la
    page, sans rechargement."""
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
# Serveur mail (SMTP) - hôte/port/chiffrement, identifiants, expéditeur,
# destinataires. Configurable depuis l'admin, sans redémarrer le service.
# N'importe quel fournisseur SMTP standard est supporté (pas seulement
# Gmail, qui reste juste la valeur par défaut historique).
# ---------------------------------------------------------------------------

@admin_bp.route("/mail-server", methods=["GET", "POST"])
def mail_server_view():
    cfg = _config()
    settings = load_settings(cfg.settings_path)
    saved = False
    test_result = None

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
            # Sinon (champ laissé vide, pas de case "effacer" cochée) : on
            # garde le mot de passe déjà enregistré tel quel - on ne veut pas
            # qu'une sauvegarde d'un autre champ efface le mot de passe juste
            # parce que le formulaire ne le réaffiche jamais en clair.

            settings.sender_name_override = request.form.get("sender_name_override", "").strip()
            settings.sender_email_override = request.form.get("sender_email_override", "").strip()
            settings.recipients_override = request.form.get("recipients_override", "").strip()
            save_settings(cfg.settings_path, settings)
            saved = True

        elif action == "test":
            smtp = settings.resolve_smtp(cfg)
            test_recipient = request.form.get("test_recipient", "").strip() or (smtp.recipients[0] if smtp.recipients else "")
            if not test_recipient:
                test_result = {"ok": False, "error": "No recipient configured to send the test to."}
            else:
                try:
                    send_test_email(smtp, test_recipient)
                    test_result = {"ok": True, "recipient": test_recipient}
                except Exception as exc:
                    logger.exception("Échec de l'envoi du mail de test")
                    test_result = {"ok": False, "error": str(exc)}

    smtp = settings.resolve_smtp(cfg)
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
    )
