"""File d'attente persistée des items détectés hors créneau horaire autorisé
(cf. schedule.py) - envoyés en un seul mail digest dès que le créneau
s'ouvre à nouveau, plutôt que perdus ou envoyés au mauvais moment."""

from __future__ import annotations

import json
from pathlib import Path


def load_pending(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def save_pending(path: str, items: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False))


def clear_pending(path: str) -> None:
    save_pending(path, [])
