"""Génère le HTML du mail avec des données factices, pour prévisualiser le
rendu dans un navigateur SANS envoyer de vrai mail ni contacter Jellyfin/Gmail.

Usage : python preview_email.py  -> écrit preview.html à côté de ce fichier
"""

import base64
from datetime import datetime
from pathlib import Path

from jellyfin_notifier.email_sender import LOGO_PATH, _build_intro, _env
from jellyfin_notifier.settings import Settings

# Pour la preview navigateur uniquement : un vrai mail utilise cid:, ici on
# encode le logo en data URI pour que ça s'affiche aussi hors client mail.
_LOGO_DATA_URI = (
    "data:image/png;base64," + base64.b64encode(LOGO_PATH.read_bytes()).decode()
    if LOGO_PATH.exists()
    else None
)

FAKE_ITEMS = [
    {
        "name": "Terminator",
        "year": 1984,
        "overview": (
            "À Los Angeles en 1984, un Terminator, cyborg surgi du futur, a pour "
            "mission d'exécuter Sarah Connor, une jeune femme dont l'enfant à "
            "naître doit sauver l'humanité."
        ),
        "type_label": "Film",
        "image_cid": None,  # pas d'image en preview (sinon cid: ne s'affiche pas hors mail)
        "deep_link": "#",
    },
    {
        "name": "Fright Night",
        "year": 2011,
        "overview": "Un ado découvre que son nouveau voisin est un vampire.",
        "type_label": "Film",
        "image_cid": None,
        "deep_link": "#",
    },
]

if __name__ == "__main__":
    settings = Settings()
    template = _env.get_template("email.html")
    html = template.render(
        items=FAKE_ITEMS,
        count=len(FAKE_ITEMS),
        date=datetime.now().strftime("%d/%m/%Y %H:%M"),
        logo_src=_LOGO_DATA_URI,
        intro_text=_build_intro(len(FAKE_ITEMS), settings),
        footer_text=settings.template_footer,
    )
    out = Path(__file__).parent / "preview.html"
    out.write_text(html, encoding="utf-8")
    print(f"Aperçu généré : {out}")
