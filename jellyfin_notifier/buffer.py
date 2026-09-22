"""Bufferise les items ajoutés pour envoyer un seul digest par rafale d'ajouts."""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class DebounceBuffer:
    """Accumule des items ; déclenche `on_flush(items)` après `delay_seconds`
    d'inactivité (chaque nouvel ajout relance le minuteur)."""

    def __init__(self, delay_seconds: int, on_flush: Callable[[list[dict]], None]):
        self._delay = delay_seconds
        self._on_flush = on_flush
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._timer: threading.Timer | None = None

    def add(self, item: dict) -> None:
        with self._lock:
            self._items.append(item)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            items, self._items = self._items, []
            self._timer = None

        if not items:
            return

        try:
            self._on_flush(items)
        except Exception:
            logger.exception("Buffer flush callback failed")
