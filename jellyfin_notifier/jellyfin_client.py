"""Small HTTP client for fetching info/images from the Jellyfin server."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, public_url: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # URL used only to build clickable links in mails (can differ from
        # base_url, e.g. a local domain name vs. the IP used for the API).
        self.public_url = (public_url or base_url).rstrip("/")

    def _auth_headers(self) -> dict:
        # This Jellyfin version only accepts the full Authorization header
        # (X-Emby-Token / X-MediaBrowser-Token return 401 on this server).
        return {"Authorization": f'MediaBrowser Client="jellyfin-notifier", Device="jellyfin-notifier", DeviceId="jellyfin-notifier", Version="1.0.0", Token="{self.api_key}"'}

    def fetch_poster(self, item_id: str, max_width: int = 400) -> bytes | None:
        """Fetches an item's poster (JPEG). None if unavailable."""
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
            logger.warning("Poster unavailable for %s (HTTP %s)", item_id, resp.status_code)
        except requests.RequestException:
            logger.exception("Network error fetching the poster for %s", item_id)
        return None

    def poster_url(self, item_id: str, max_width: int = 400) -> str | None:
        """Direct URL (auth via query param) to an item's poster - used
        ONLY for the browser preview (the admin), which can't display the
        `cid:` references used in the real mail (those only work in a mail
        client, not in a browser <img>)."""
        if not item_id:
            return None
        return f"{self.public_url}/Items/{item_id}/Images/Primary?maxWidth={max_width}&api_key={self.api_key}"

    def deep_link(self, item_id: str) -> str:
        """Direct link to the item's page in the Jellyfin web client
        (uses public_url, meant to be clicked from a mail)."""
        return f"{self.public_url}/web/index.html#!/details?id={item_id}"

    def fetch_recent_items(self, item_types: set[str], limit: int = 200) -> list[dict]:
        """Fetches the N most recently added items in the library (sorted
        by date added, most recent first).

        Secondary sort by SortName: needed for a STABLE order between two
        calls when several items share the exact same DateCreated (e.g. a
        bulk import) - without it, Jellyfin can return a slightly different
        order from one call to the next for tied items, which makes an old
        item "enter/leave" the window and get wrongly flagged as new. (Id
        isn't a valid sort field for Jellyfin -> 400 Bad Request, hence
        SortName.)

        Limit of 200 (instead of 40): the secondary sort alone isn't enough
        when a bulk-import batch contains more items than the window - they
        then float on the boundary and get wrongly flagged as new from one
        poll to the next. A wide window absorbs the whole batch once and
        for all."""
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
