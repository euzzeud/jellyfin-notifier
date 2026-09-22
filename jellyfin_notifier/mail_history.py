"""Historique des mails envoyés (notifications 'New Content', annonces
'Upcoming', mails de test), persisté en JSON avec le même principe que
pending.json/upcoming.json : donne à l'admin un vrai journal consultable
(page 'Mail history') plutôt que juste le statut du dernier envoi."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()

# Assez pour couvrir plusieurs semaines d'activité typique sans laisser le
# fichier grossir indéfiniment sur une install qui tourne des mois.
MAX_ENTRIES = 300


def record(
    path: str,
    *,
    scope: str,  # "new" | "upcoming" | "test"
    subject: str,
    recipients: list[str],
    item_names: list[str],
    success: bool,
    error: str | None = None,
) -> None:
    entry = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": scope,
        "subject": subject,
        "recipients": recipients,
        "item_names": item_names,
        "success": success,
        "error": error,
    }
    p = Path(path)
    with _lock:
        history = _load(p)
        history.append(entry)
        if len(history) > MAX_ENTRIES:
            history = history[-MAX_ENTRIES:]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def _load(p: Path) -> list[dict]:
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def list_history(path: str, limit: int = 200) -> list[dict]:
    """Les plus récents d'abord."""
    history = _load(Path(path))
    return list(reversed(history))[:limit]
