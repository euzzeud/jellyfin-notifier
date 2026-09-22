"""JSON CRUD for "upcoming" titles announced manually from the admin
(movies/shows not yet present in the Jellyfin library), with support for
a manually uploaded poster (no Jellyfin item_id -> no poster retrievable
via the API)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

UPLOADS_DIR = Path(__file__).parent / "uploads" / "upcoming"
ALLOWED_POSTER_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def _load(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save(path: str, items: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def list_upcoming(path: str) -> list[dict]:
    return _load(path)


def add_upcoming(path: str, name: str, year: str | None, note: str | None, type_label: str) -> dict:
    items = _load(path)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "year": year or None,
        "note": note or "",
        "type_label": type_label or "Movie",
        "poster_filename": None,
        "added_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    items.append(entry)
    _save(path, items)
    return entry


def delete_upcoming(path: str, item_id: str) -> None:
    items = _load(path)
    remaining = []
    for entry in items:
        if entry.get("id") == item_id:
            poster_filename = entry.get("poster_filename")
            if poster_filename:
                (UPLOADS_DIR / poster_filename).unlink(missing_ok=True)
            continue
        remaining.append(entry)
    _save(path, remaining)


def get_many(path: str, item_ids: list[str]) -> list[dict]:
    wanted = set(item_ids)
    return [i for i in _load(path) if i.get("id") in wanted]


def save_poster(path: str, item_id: str, filename: str, content: bytes) -> str | None:
    """Saves an uploaded poster for an existing entry, replacing the old
    one if present. Returns the stored filename, or None if the extension
    isn't allowed or the entry can't be found."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_POSTER_EXTENSIONS:
        return None

    items = _load(path)
    entry = next((i for i in items if i.get("id") == item_id), None)
    if entry is None:
        return None

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    old_filename = entry.get("poster_filename")
    if old_filename:
        (UPLOADS_DIR / old_filename).unlink(missing_ok=True)

    stored_filename = f"{item_id}.{ext}"
    (UPLOADS_DIR / stored_filename).write_bytes(content)
    entry["poster_filename"] = stored_filename
    _save(path, items)
    return stored_filename


def item_from_upcoming(entry: dict) -> dict:
    """Converts an 'upcoming' entry to the same internal format as
    item_from_api/item_from_payload, to reuse the existing mail-sending
    code. No item_id -> no Jellyfin poster or deep_link; an uploaded
    poster (poster_filename) is injected separately by admin.py (it needs
    to know the URL/disk path, which are specific to the current HTTP
    request)."""
    note = entry.get("note") or ""
    year = entry.get("year")
    return {
        "item_id": None,
        "item_type": entry.get("type_label"),
        "name": entry.get("name", "Untitled"),
        "year": int(year) if year else None,
        "overview": note or "Coming soon to Jellyfin.",
        "genres": None,
        "community_rating": None,
        "run_time_ticks": None,
        "type_label": f"Coming soon • {entry.get('type_label') or 'Movie'}",
    }
