"""Petit client HTTP pour aller chercher des infos/images sur le serveur Jellyfin."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, public_url: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # URL utilisée uniquement pour construire les liens cliquables dans les mails
        # (peut différer de base_url, ex: nom de domaine local vs IP utilisée pour l'API).
        self.public_url = (public_url or base_url).rstrip("/")

    def _auth_headers(self) -> dict:
        # Cette version de Jellyfin n'accepte que le header Authorization complet
        # (X-Emby-Token / X-MediaBrowser-Token renvoient 401 sur ce serveur).
        return {"Authorization": f'MediaBrowser Client="jellyfin-notifier", Device="jellyfin-notifier", DeviceId="jellyfin-notifier", Version="1.0.0", Token="{self.api_key}"'}

    def fetch_poster(self, item_id: str, max_width: int = 400) -> bytes | None:
        """Récupère l'affiche (JPEG) d'un item. None si indisponible."""
        if not self.api_key or not item_id:
            return None
        try:
            resp = requests.get(
                f"{self.base_url}/Items/{item_id}/Images/Primary",
                headers=self._auth_headers(),
                params={"maxWidth": max_width, "quality": 85},
                timeout=10,
            )
            if resp.ok:
                return resp.content
            logger.warning("Poster indisponible pour %s (HTTP %s)", item_id, resp.status_code)
        except requests.RequestException:
            logger.exception("Erreur réseau en récupérant le poster de %s", item_id)
        return None

    def poster_url(self, item_id: str, max_width: int = 400) -> str | None:
        """URL directe (auth par query param) vers l'affiche d'un item -
        utilisée UNIQUEMENT pour l'aperçu dans le navigateur (l'admin), qui ne
        peut pas afficher les `cid:` utilisés dans le vrai mail (ceux-ci ne
        fonctionnent que dans un client mail, pas dans un <img> de navigateur)."""
        if not item_id:
            return None
        return f"{self.public_url}/Items/{item_id}/Images/Primary?maxWidth={max_width}&api_key={self.api_key}"

    def deep_link(self, item_id: str) -> str:
        """Lien direct vers la fiche de l'item dans le client web Jellyfin
        (utilise public_url, pensé pour être cliqué depuis un mail)."""
        return f"{self.public_url}/web/index.html#!/details?id={item_id}"

    def fetch_recent_items(self, item_types: set[str], limit: int = 200) -> list[dict]:
        """Récupère les N items les plus récemment ajoutés à la bibliothèque
        (triés par date d'ajout, du plus récent au plus ancien).

        Tri secondaire par SortName : nécessaire pour un ordre STABLE entre deux
        appels quand plusieurs items partagent exactement le même DateCreated
        (ex: import en masse) - sans ça, Jellyfin peut renvoyer un ordre
        légèrement différent d'un appel à l'autre pour les items à égalité,
        ce qui fait "entrer/sortir" un vieil item de la fenêtre et le fait
        signaler à tort comme nouveau. (Id n'est pas un champ de tri valide
        pour Jellyfin -> 400 Bad Request, d'où SortName.)

        Limit à 200 (au lieu de 40) : le tri secondaire seul ne suffit pas
        quand un lot d'import en masse contient plus d'items que la fenêtre -
        ils flottent alors sur la frontière et se signalent à tort comme
        nouveaux d'un poll à l'autre. Une fenêtre large absorbe tout le lot
        une bonne fois pour toutes."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": ",".join(sorted(item_types)),
            "SortBy": "DateCreated,SortName",
            "SortOrder": "Descending,Ascending",
            "Limit": limit,
            "Fields": "Overview,ProductionYear,Genres,CommunityRating,RunTimeTicks",
        }
        resp = requests.get(
            f"{self.base_url}/Items",
            headers=self._auth_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("Items", [])
