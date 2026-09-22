"""History of sent mails ('New Content' notifications, 'Upcoming'
announcements, test mails), persisted as JSON on the same principle as
pending.json/upcoming.json: gives the admin a real, browsable log (the
'Mail History' page) instead of just the last send's status."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from . import metrics

_lock = threading.Lock()

# Enough to cover several weeks of typical activity without letting the
# file grow indefinitely on an install that runs for months.
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
    # Cumulative counters for /metrics (never trimmed to MAX_ENTRIES,
    # unlike this JSON log) - a single call site since send_email()/
    # send_test_email() both go through record().
    metrics.inc_mail(scope, success)


def _load(p: Path) -> list[dict]:
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def list_history(path: str, limit: int = 200) -> list[dict]:
    """Most recent first."""
    history = _load(Path(path))
    return list(reversed(history))[:limit]
