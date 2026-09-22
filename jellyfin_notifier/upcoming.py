"""CRUD JSON pour les titres "à venir" annoncés manuellement depuis l'admin
(films/séries pas encore présents dans la bibliothèque Jellyfin)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path


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
        "type_label": type_label or "Film",
        "added_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    items.append(entry)
    _save(path, items)
    return entry


def delete_upcoming(path: str, item_id: str) -> None:
    items = [i for i in _load(path) if i.get("id") != item_id]
    _save(path, items)


def get_many(path: str, item_ids: list[str]) -> list[dict]:
    wanted = set(item_ids)
    return [i for i in _load(path) if i.get("id") in wanted]


def item_from_upcoming(entry: dict) -> dict:
    """Convertit une entrée 'à venir' au même format interne que
    item_from_api/item_from_payload, pour réutiliser l'envoi de mail
    existant. Pas d'item_id -> pas de poster ni de bouton "Regarder"
    (le template masque déjà le bouton quand deep_link est absent)."""
    note = entry.get("note") or ""
    year = entry.get("year")
    return {
        "item_id": None,
        "item_type": entry.get("type_label"),
        "name": entry.get("name", "Sans titre"),
        "year": int(year) if year else None,
        "overview": note or "Bientôt disponible sur Jellyfin.",
        "genres": None,
        "community_rating": None,
        "run_time_ticks": None,
        "type_label": f"Bientôt • {entry.get('type_label') or 'Film'}",
    }
