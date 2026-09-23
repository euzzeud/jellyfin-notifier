"""Generates the mail's HTML with fake data, to preview the render in a
browser WITHOUT sending a real mail or contacting Jellyfin/the mail server.

Usage: python preview_email.py  -> writes preview.html next to this file
"""

import base64
from datetime import datetime
from pathlib import Path

from jellyfin_notifier.email_sender import LOGO_PATH, _build_intro, _env
from jellyfin_notifier.settings import Settings

# For the browser preview only: a real mail uses cid:, here the logo is
# encoded as a data URI so it also displays outside a mail client.
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
            "In Los Angeles in 1984, a Terminator, a cyborg from the future, is "
            "on a mission to kill Sarah Connor, a young woman whose unborn "
            "child will one day save humanity."
        ),
        "type_label": "Movie",
        "image_cid": None,  # no image in preview (otherwise cid: doesn't display outside a mail client)
        "deep_link": "#",
    },
    {
        "name": "Fright Night",
        "year": 2011,
        "overview": "A teenager discovers that his new neighbor is a vampire.",
        "type_label": "Movie",
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
    print(f"Preview generated: {out}")
