"""State of the browser extension: heartbeat, open AI tabs, selected target tabs."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from ..core.providers import PROVIDERS
from .logs import LogBuffer


class ExtensionState:
    def __init__(self, logs: LogBuffer, heartbeat_ttl: float, busy_heartbeat_ttl: float, auto_config: bool):
        self.logs = logs
        self.heartbeat_ttl = heartbeat_ttl
        self.busy_heartbeat_ttl = busy_heartbeat_ttl
        self.auto_config = auto_config
        self.tabs: List[Dict[str, Any]] = []
        self.target_tabs: Dict[str, int] = {}
        self.busy = False
        self.last_seen = 0.0
        self.active_polls = 0  # GET /job long-polls currently open
        self.last_extraction: Optional[Dict[str, Any]] = None  # {source, bridge, at}
        self._seen_once = False
        self._online = asyncio.Event()

    # ----------------------------------------------------------- heartbeat
    def touch(self) -> None:
        """Called on every request coming from the extension."""
        self.last_seen = time.time()
        self._seen_once = True
        self._online.set()

    def is_connected(self, has_active_jobs: bool = False) -> bool:
        if not self._seen_once:
            return False
        if self.active_polls > 0:
            return True
        age = time.time() - self.last_seen
        if age < self.heartbeat_ttl:
            return True
        # While the extension is busy running a job (fresh-chat navigation,
        # waiting for the composer…) its poll loop is blocked: be tolerant.
        return (self.busy or has_active_jobs) and age < self.busy_heartbeat_ttl

    async def wait_connected(self, timeout: float, has_active_jobs: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        while not self.is_connected(has_active_jobs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._online.clear()
            try:
                await asyncio.wait_for(self._online.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                pass
        return True

    # ---------------------------------------------------------------- tabs
    def update_tabs(self, tabs: List[Dict[str, Any]], busy: bool) -> None:
        self.touch()
        self.tabs = [t for t in tabs if isinstance(t, dict)]
        self.busy = bool(busy)

        # Drop selections pointing to tabs that were closed (v4.0 kept them
        # forever, so auto-config never picked the newly opened tab).
        open_ids = {(t.get("provider"), t.get("id")) for t in self.tabs}
        for provider, tab_id in list(self.target_tabs.items()):
            if (provider, tab_id) not in open_ids:
                del self.target_tabs[provider]
                self.logs.warn(f"Onglet {provider} {tab_id} fermé → sélection retirée")

        if self.auto_config:
            for provider in PROVIDERS:
                if provider in self.target_tabs:
                    continue
                tab = next((t for t in self.tabs if t.get("provider") == provider), None)
                if tab and isinstance(tab.get("id"), int):
                    self.target_tabs[provider] = tab["id"]
                    self.logs.info(f"Auto-configured {provider} tab: {tab['id']}")

    def select_tab(self, provider: str, tab_id: int) -> bool:
        if not any(t.get("id") == tab_id and t.get("provider") == provider for t in self.tabs):
            return False
        self.target_tabs[provider] = tab_id
        self.logs.info(f"Selected {provider} tab: {tab_id}")
        return True

    def tab_for(self, provider: str) -> Optional[int]:
        return self.target_tabs.get(provider)

    def providers_ready(self) -> Dict[str, bool]:
        return {
            p: (p in self.target_tabs and any(t.get("provider") == p for t in self.tabs))
            for p in PROVIDERS
        }
