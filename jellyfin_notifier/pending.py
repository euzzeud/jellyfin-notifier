"""Persisted queue of items detected outside the allowed send window (see
schedule.py) - sent as a single digest mail as soon as the window opens
again, instead of being lost or sent at the wrong time."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .atomic_json import atomic_write_text

# Guards against the background poller thread and a manually-triggered
# "Poll now" (runs on the Flask request thread) both calling save_pending()
# at once - see poller.py's own _poll_lock for the broader fix (preventing
# two poll cycles from running concurrently in the first place).
_lock = threading.Lock()


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
    with _lock:
        atomic_write_text(p, json.dumps(items, ensure_ascii=False))


def clear_pending(path: str) -> None:
    save_pending(path, [])
