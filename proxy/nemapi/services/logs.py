"""In-memory ring buffer of events shown in the admin UI (+ console output)."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List

logger = logging.getLogger("nemapi")

_LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


class LogBuffer:
    def __init__(self, maxlen: int = 120):
        self._items: Deque[Dict[str, str]] = deque(maxlen=maxlen)

    def add(self, message: str, level: str = "info") -> None:
        level = level if level in _LEVELS else "info"
        self._items.append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": message})
        logger.log(_LEVELS[level], message)

    def info(self, message: str) -> None:
        self.add(message, "info")

    def warn(self, message: str) -> None:
        self.add(message, "warn")

    def error(self, message: str) -> None:
        self.add(message, "error")

    def items(self) -> List[Dict[str, str]]:
        return list(self._items)

    def last_error(self, window: int = 8):
        recent = list(self._items)[-window:]
        return next((e for e in reversed(recent) if e["level"] == "error"), None)
