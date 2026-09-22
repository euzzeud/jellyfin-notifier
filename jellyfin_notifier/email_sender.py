"""Construction et envoi du mail (thème Jellyfin) via SMTP - Gmail par
défaut, mais n'importe quel serveur SMTP standard (STARTTLS, SSL implicite,
ou sans chiffrement) fonctionne, cf. Settings.resolve_smtp()."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import mail_history
from .config import Config, SmtpSettings
from .jellyfin_client import JellyfinClient
from .settings import ScopedEmailSettings, Settings

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGO_PATH = Path(__file__).parent / "assets" / "jellyfin-logo.png"
LOGO_CID = "jellyfin_logo"
DEFAULT_TEMPLATE_NAME = "email.html"
OVERVIEW_MAX_LENGTH = 200  # fallback si aucun Settings n'est fourni

EmailSettings = Settings | ScopedEmailSettings

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
    settings: EmailSettings,
    for_preview: bool,
) -> tuple[list[dict], list[MIMEImage]]:
    """Normalise les items pour le rendu. En mode aperçu navigateur
    (for_preview=True), utilise des URLs directes vers Jellyfin (ou une image
    uploadée manuellement, cf. `_poster_url`/`_local_poster_path`) au lieu de
    pièces jointes `cid:`, qui ne s'affichent que dans un client mail, jamais
    dans un <img> de navigateur."""
    render_items = []
    inline_images = []

    for idx, item in enumerate(items):
        cid = None
        # `_poster_url` : image déjà connue par l'appelant (ex: affiche
        # uploadée pour un titre "à venir") - prioritaire sur Jellyfin.
        image_url = item.get("_poster_url")

        if for_preview:
            if not image_url and item.get("item_id"):
                image_url = jf_client.poster_url(item["item_id"])
        else:
            image_bytes = None
            local_path = item.get("_local_poster_path")
            if local_path and Path(local_path).exists():
                image_bytes = Path(local_path).read_bytes()
            elif item.get("item_id"):
                image_bytes = jf_client.fetch_poster(item["item_id"])
            if image_bytes:
                cid = f"poster{idx}"
                img = MIMEImage(image_bytes)
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
                inline_images.append(img)

        deep_link = jf_client.deep_link(item["item_id"]) if item.get("item_id") else None
        if deep_link is None and for_preview and item.get("_fake_deep_link"):
            # Aperçu navigateur d'un item d'exemple sans item_id réel : on
            # affiche quand même le bouton "Regarder" (lien factice) pour que
            # l'admin voie à quoi ressemblera le mail final.
            deep_link = item["_fake_deep_link"]

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
                "deep_link": deep_link,
            }
        )
    return render_items, inline_images


def _prepare_content(
    items: list[dict],
    jf_client: JellyfinClient,
    settings: EmailSettings | None = None,
    template_name: str = DEFAULT_TEMPLATE_NAME,
) -> tuple[str, list[MIMEImage]]:
    """Prépare le HTML rendu et les images inline (logo + posters), une seule
    fois, pour être réutilisés pour chaque destinataire (évite de refaire les
    appels API Jellyfin/posters une fois par destinataire). Utilisé pour le
    VRAI envoi de mail (cid: pour les images). `template_name` sélectionne le
    template dans templates/ (email.html pour les nouveaux contenus,
    email_upcoming.html pour les annonces "à venir")."""
    settings = settings or Settings()
    inline_images = []

    if LOGO_PATH.exists():
        logo_img = MIMEImage(LOGO_PATH.read_bytes())
        logo_img.add_header("Content-ID", f"<{LOGO_CID}>")
        logo_img.add_header("Content-Disposition", "inline", filename="jellyfin-logo.png")
        inline_images.append(logo_img)

    render_items, item_images = _build_render_items(items, jf_client, settings, for_preview=False)
    inline_images.extend(item_images)

    template = _env.get_template(template_name)
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
    settings: EmailSettings | None = None,
    raw_source: str | None = None,
    logo_url: str | None = None,
    template_name: str = DEFAULT_TEMPLATE_NAME,
) -> str:
    """Rendu HTML pour affichage dans le navigateur (admin), donc SANS pièces
    jointes `cid:`. `raw_source`, si fourni, permet de prévisualiser un
    template en cours d'édition, PAS ENCORE sauvegardé sur disque."""
    settings = settings or Settings()
    render_items, _ = _build_render_items(items, jf_client, settings, for_preview=True)

    template = _env.from_string(raw_source) if raw_source is not None else _env.get_template(template_name)
    return template.render(
        items=render_items,
        count=len(render_items),
        date=datetime.now().strftime("%d/%m/%Y %H:%M"),
        logo_src=logo_url,
        intro_text=_build_intro(len(render_items), settings),
        footer_text=settings.template_footer,
        **_color_kwargs(settings),
    )


def _default_smtp(config: Config) -> SmtpSettings:
    """SmtpSettings basé uniquement sur Config (.env) - utilisé quand
    l'appelant ne fournit pas explicitement les surcharges de Settings
    (ex: webhook.py, ou tout appel sans admin en cours)."""
    return SmtpSettings(
        host=config.smtp_host,
        port=config.smtp_port,
        encryption=config.smtp_encryption,
        username=config.smtp_username,
        password=config.smtp_password,
        sender_name=config.sender_name,
        sender_email=config.sender_email,
        recipients=config.recipients,
    )


def build_email(
    items: list[dict],
    smtp: SmtpSettings,
    recipient: str,
    html: str,
    inline_images: list[MIMEImage],
    settings: EmailSettings | None = None,
) -> MIMEMultipart:
    """Construit un message pour UN SEUL destinataire (chacun ne voit que sa
    propre adresse dans le header To - avant, tous les destinataires étaient
    listés ensemble)."""
    settings = settings or Settings()
    msg = MIMEMultipart("related")
    msg["Subject"] = _build_subject(items, settings)
    msg["From"] = f"{smtp.sender_name} <{smtp.sender_email}>"
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
    settings: EmailSettings | None = None,
    smtp: SmtpSettings | None = None,
    recipients: list[str] | None = None,
    template_name: str = DEFAULT_TEMPLATE_NAME,
) -> None:
    """Envoie le mail via SMTP. `smtp`, si fourni, prévaut sur Config
    (permet aux appelants qui ont accès aux Settings courants de fusionner
    les surcharges éditées depuis l'admin - cf. Settings.resolve_smtp()).
    Sans `smtp`, retombe sur les valeurs figées de Config (.env)."""
    settings = settings or Settings()
    smtp = smtp or _default_smtp(config)
    recipients = recipients if recipients is not None else smtp.recipients
    html, inline_images = _prepare_content(items, jf_client, settings, template_name=template_name)
    subject = _build_subject(items, settings)
    # "upcoming" est le seul autre template utilisé en pratique (cf. les
    # appels avec template_name="email_upcoming.html") - inféré ici plutôt
    # que de faire remonter un paramètre "scope" jusqu'à tous les appelants.
    scope = "upcoming" if template_name != DEFAULT_TEMPLATE_NAME else "new"

    try:
        smtp_cls = smtplib.SMTP_SSL if smtp.encryption == "ssl" else smtplib.SMTP
        with smtp_cls(smtp.host, smtp.port, timeout=20) as server:
            if smtp.encryption == "starttls":
                server.starttls()
            if smtp.username:
                server.login(smtp.username, smtp.password)
            for recipient in recipients:
                msg = build_email(items, smtp, recipient, html, inline_images, settings)
                server.sendmail(smtp.sender_email, [recipient], msg.as_string())
    except Exception as exc:
        mail_history.record(
            config.mail_history_path, scope=scope, subject=subject, recipients=recipients,
            item_names=[i.get("name", "?") for i in items], success=False, error=str(exc),
        )
        raise
    logger.info("Mail sent to %d recipient(s) for %d item(s)", len(recipients), len(items))
    mail_history.record(
        config.mail_history_path, scope=scope, subject=subject, recipients=recipients,
        item_names=[i.get("name", "?") for i in items], success=True,
    )


def send_test_email(smtp: SmtpSettings, to: str, history_path: str | None = None) -> None:
    """Envoie un mail de test minimal (texte brut, sans Jellyfin ni
    template) - utilisé par la page "Mail server" de l'admin pour vérifier
    une config SMTP avant de compter dessus pour les vraies notifs."""
    subject = "Test - Jellyfin Notifier"
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = f"{smtp.sender_name} <{smtp.sender_email}>"
    msg["To"] = to
    msg.attach(
        MIMEText(
            "This is a test mail sent from the Jellyfin Notifier admin interface.\n"
            "If you received this message, the SMTP server configuration is working.",
            "plain",
            "utf-8",
        )
    )
    try:
        smtp_cls = smtplib.SMTP_SSL if smtp.encryption == "ssl" else smtplib.SMTP
        with smtp_cls(smtp.host, smtp.port, timeout=20) as server:
            if smtp.encryption == "starttls":
                server.starttls()
            if smtp.username:
                server.login(smtp.username, smtp.password)
            server.sendmail(smtp.sender_email, [to], msg.as_string())
    except Exception as exc:
        if history_path:
            mail_history.record(history_path, scope="test", subject=subject, recipients=[to], item_names=[], success=False, error=str(exc))
        raise
    if history_path:
        mail_history.record(history_path, scope="test", subject=subject, recipients=[to], item_names=[], success=True)
