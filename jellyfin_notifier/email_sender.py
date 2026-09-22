"""Construction et envoi du mail (thème Jellyfin) via SMTP Gmail."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .jellyfin_client import JellyfinClient
from .settings import Settings

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGO_PATH = Path(__file__).parent / "assets" / "jellyfin-logo.png"
LOGO_CID = "jellyfin_logo"
OVERVIEW_MAX_LENGTH = 200  # fallback si aucun Settings n'est fourni

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _truncate_overview(overview: str | None, max_length: int = OVERVIEW_MAX_LENGTH) -> str | None:
    """Coupe le synopsis pour éviter les mails à rallonge qui spoilent tout le
    film - coupe sur le dernier espace avant la limite plutôt qu'en plein mot."""
    if not overview or len(overview) <= max_length:
        return overview
    truncated = overview[:max_length].rsplit(" ", 1)[0]
    return f"{truncated}…"


def _format_rating(rating: float | None) -> str | None:
    if rating is None:
        return None
    return f"{rating:.1f}"


def _format_genres(genres: list[str] | None, max_genres: int = 3) -> str | None:
    if not genres:
        return None
    return " · ".join(genres[:max_genres])


def _format_duration(run_time_ticks: int | None) -> str | None:
    """Convertit les RunTimeTicks Jellyfin (unités de 100ns) en durée lisible
    (ex: "1h47" ou "45 min")."""
    if not run_time_ticks:
        return None
    total_minutes = round(run_time_ticks / 10_000_000 / 60)
    if total_minutes <= 0:
        return None
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}" if minutes else f"{hours}h"
    return f"{minutes} min"


def _type_label(item_type: str, payload: dict) -> str:
    if item_type == "Movie":
        return "Film"
    if item_type == "Series":
        return "Série"
    if item_type == "Season":
        series = payload.get("SeriesName", "")
        season_num = payload.get("SeasonNumber00") or payload.get("SeasonNumber")
        return f"Saison {season_num} — {series}".strip(" —")
    if item_type == "Episode":
        series = payload.get("SeriesName", "")
        return f"Épisode — {series}".strip(" —")
    return item_type or "Contenu"


def item_from_payload(payload: dict) -> dict:
    """Normalise le payload brut du webhook Jellyfin en dict interne."""
    return {
        "item_id": payload.get("ItemId"),
        "item_type": payload.get("ItemType"),
        "name": payload.get("Name", "Sans titre"),
        "year": payload.get("Year"),
        "overview": payload.get("Overview"),
        "type_label": _type_label(payload.get("ItemType"), payload),
    }


def item_from_api(item: dict) -> dict:
    """Normalise un item renvoyé par l'API Jellyfin (/Items) en dict interne.
    Utilisé par le poller, en remplacement du webhook."""
    item_type = item.get("Type")
    return {
        "item_id": item.get("Id"),
        "item_type": item_type,
        "name": item.get("Name", "Sans titre"),
        "year": item.get("ProductionYear"),
        "overview": item.get("Overview"),
        "genres": item.get("Genres"),
        "community_rating": item.get("CommunityRating"),
        "run_time_ticks": item.get("RunTimeTicks"),
        "type_label": _type_label(
            item_type,
            {
                "SeriesName": item.get("SeriesName"),
                "SeasonNumber00": item.get("ParentIndexNumber"),
            },
        ),
    }


def _build_subject(items: list[dict], settings: Settings) -> str:
    if len(items) == 1:
        return settings.template_subject_single.format(name=items[0]["name"])
    return settings.template_subject_multi.format(count=len(items))


def _build_intro(count: int, settings: Settings) -> str:
    if count == 1:
        return settings.template_intro_single.format(count=count)
    return settings.template_intro_multi.format(count=count)


def _color_kwargs(settings: Settings) -> dict:
    return {
        "color_bg": settings.color_bg,
        "color_card": settings.color_card,
        "color_header": settings.color_header,
        "color_accent": settings.color_accent,
        "color_button": settings.color_button,
        "color_text": settings.color_text,
        "color_muted": settings.color_muted,
    }


def _build_render_items(
    items: list[dict],
    jf_client: JellyfinClient,
    settings: Settings,
    for_preview: bool,
) -> tuple[list[dict], list[MIMEImage]]:
    """Normalise les items pour le rendu. En mode aperçu navigateur
    (for_preview=True), utilise des URLs directes vers Jellyfin pour les
    affiches au lieu de pièces jointes `cid:` (qui ne s'affichent que dans un
    client mail, jamais dans un <img> de navigateur)."""
    render_items = []
    inline_images = []

    for idx, item in enumerate(items):
        cid = None
        image_url = None
        if for_preview:
            image_url = jf_client.poster_url(item["item_id"]) if item.get("item_id") else None
        else:
            image_bytes = jf_client.fetch_poster(item["item_id"]) if item.get("item_id") else None
            if image_bytes:
                cid = f"poster{idx}"
                img = MIMEImage(image_bytes)
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
                inline_images.append(img)

        render_items.append(
            {
                "name": item["name"],
                "year": item.get("year"),
                "overview": _truncate_overview(item.get("overview"), settings.overview_max_length),
                "type_label": item.get("type_label"),
                "genres": _format_genres(item.get("genres")),
                "rating": _format_rating(item.get("community_rating")),
                "duration": _format_duration(item.get("run_time_ticks")),
                "image_cid": cid,
                "image_url": image_url,
                "deep_link": jf_client.deep_link(item["item_id"]) if item.get("item_id") else None,
            }
        )
    return render_items, inline_images


def _prepare_content(
    items: list[dict],
    jf_client: JellyfinClient,
    settings: Settings | None = None,
) -> tuple[str, list[MIMEImage]]:
    """Prépare le HTML rendu et les images inline (logo + posters), une seule
    fois, pour être réutilisés pour chaque destinataire (évite de refaire les
    appels API Jellyfin/posters une fois par destinataire). Utilisé pour le
    VRAI envoi de mail (cid: pour les images)."""
    settings = settings or Settings()
    inline_images = []

    if LOGO_PATH.exists():
        logo_img = MIMEImage(LOGO_PATH.read_bytes())
        logo_img.add_header("Content-ID", f"<{LOGO_CID}>")
        logo_img.add_header("Content-Disposition", "inline", filename="jellyfin-logo.png")
        inline_images.append(logo_img)

    render_items, item_images = _build_render_items(items, jf_client, settings, for_preview=False)
    inline_images.extend(item_images)

    template = _env.get_template("email.html")
    html = template.render(
        items=render_items,
        count=len(render_items),
        date=datetime.now().strftime("%d/%m/%Y %H:%M"),
        logo_src=f"cid:{LOGO_CID}" if LOGO_PATH.exists() else None,
        intro_text=_build_intro(len(render_items), settings),
        footer_text=settings.template_footer,
        **_color_kwargs(settings),
    )
    return html, inline_images


def render_preview_html(
    items: list[dict],
    jf_client: JellyfinClient,
    settings: Settings | None = None,
    raw_source: str | None = None,
    logo_url: str | None = None,
) -> str:
    """Rendu HTML pour affichage dans le navigateur (admin), donc SANS pièces
    jointes `cid:`. `raw_source`, si fourni, permet de prévisualiser un
    template en cours d'édition, PAS ENCORE sauvegardé sur disque."""
    settings = settings or Settings()
    render_items, _ = _build_render_items(items, jf_client, settings, for_preview=True)

    template = _env.from_string(raw_source) if raw_source is not None else _env.get_template("email.html")
    return template.render(
        items=render_items,
        count=len(render_items),
        date=datetime.now().strftime("%d/%m/%Y %H:%M"),
        logo_src=logo_url,
        intro_text=_build_intro(len(render_items), settings),
        footer_text=settings.template_footer,
        **_color_kwargs(settings),
    )


def build_email(
    items: list[dict],
    config: Config,
    recipient: str,
    html: str,
    inline_images: list[MIMEImage],
    settings: Settings | None = None,
) -> MIMEMultipart:
    """Construit un message pour UN SEUL destinataire (chacun ne voit que sa
    propre adresse dans le header To - avant, tous les destinataires étaient
    listés ensemble)."""
    settings = settings or Settings()
    msg = MIMEMultipart("related")
    msg["Subject"] = _build_subject(items, settings)
    msg["From"] = f"{config.sender_name} <{config.gmail_address}>"
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(html, "html", "utf-8"))

    for img in inline_images:
        # Chaque image ne peut être attachée qu'à un seul message MIME à la
        # fois : on en refait une copie légère pour chaque destinataire.
        img_copy = MIMEImage(img.get_payload(decode=True))
        img_copy.add_header("Content-ID", img["Content-ID"])
        img_copy.add_header("Content-Disposition", img["Content-Disposition"])
        msg.attach(img_copy)

    return msg


def send_email(
    items: list[dict],
    config: Config,
    jf_client: JellyfinClient,
    settings: Settings | None = None,
    recipients: list[str] | None = None,
) -> None:
    settings = settings or Settings()
    recipients = recipients if recipients is not None else config.recipients
    html, inline_images = _prepare_content(items, jf_client, settings)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(config.gmail_address, config.gmail_app_password)
        for recipient in recipients:
            msg = build_email(items, config, recipient, html, inline_images, settings)
            server.sendmail(config.gmail_address, [recipient], msg.as_string())
    logger.info("Mail envoyé à %d destinataire(s) pour %d item(s)", len(recipients), len(items))
