"""Persisted queue of items detected outside the allowed send window (see
schedule.py) - sent as a single digest mail as soon as the window opens
again, instead of being lost or sent at the wrong time."""

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
