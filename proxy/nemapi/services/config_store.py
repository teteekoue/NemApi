"""Persistent configuration: proxy settings + API keys (data/config.json)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.jsonfile import read_json, write_json_atomic
from .logs import LogBuffer

DEFAULT_SETTINGS: Dict[str, bool] = {
    "stream_enabled": True,
    "auto_config": True,
    "fresh_chat": True,
    "premium_md": True,
}
# v4.2–4.5 settings now owned by the agent integrations (services/integrations.py)
LEGACY_SETTINGS = ("incremental_context", "side_queries_local")


def generate_api_key() -> str:
    return f"nemapi-token{secrets.token_urlsafe(24)}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ConfigStore:
    def __init__(self, path: Path, logs: LogBuffer):
        self.path = path
        self.logs = logs
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._load()

    # ------------------------------------------------------------------ io
    def _defaults(self) -> Dict[str, Any]:
        return {
            "api_keys": {},
            "settings": dict(DEFAULT_SETTINGS),
            "integrations": {},
            # provider id → agent id that owns the web discussion (the agent
            # that never opens a new one). Remembered so a client that does
            # not identify itself on every request keeps its integration.
            "discussion_owners": {},
        }

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._defaults()
        try:
            data = read_json(self.path)
            if not isinstance(data, dict):
                raise ValueError("config root must be an object")
        except (OSError, ValueError) as exc:
            self.logs.error(f"Erreur de chargement de la config: {exc}")
            return self._defaults()
        if not isinstance(data.get("api_keys"), dict):
            data["api_keys"] = {}
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        legacy = [k for k in LEGACY_SETTINGS if k in settings]
        if legacy:
            self.logs.info(
                "Config: " + ", ".join(legacy) + " → déplacé(s) dans « Intégration agents IA » (désactivé par défaut)"
            )
        data["settings"] = {**DEFAULT_SETTINGS, **{k: bool(v) for k, v in settings.items() if k in DEFAULT_SETTINGS}}
        if not isinstance(data.get("integrations"), dict):
            data["integrations"] = {}
        if not isinstance(data.get("discussion_owners"), dict):
            data["discussion_owners"] = {}
        return data

    def save(self) -> None:
        with self._lock:
            try:
                write_json_atomic(self.path, self._data)
            except OSError as exc:
                self.logs.error(f"Erreur de sauvegarde de la config: {exc}")

    # ------------------------------------------------------------ settings
    @property
    def settings(self) -> Dict[str, bool]:
        with self._lock:
            return dict(self._data["settings"])

    def get(self, name: str) -> bool:
        with self._lock:
            return bool(self._data["settings"].get(name, DEFAULT_SETTINGS.get(name, False)))

    def update_settings(self, changes: Dict[str, Optional[bool]]) -> Dict[str, bool]:
        labels = {
            "stream_enabled": "Stream",
            "auto_config": "Auto-config",
            "fresh_chat": "Fresh-chat",
            "premium_md": "Premium-MD",
        }
        changed = False
        with self._lock:
            for key, value in changes.items():
                if key in DEFAULT_SETTINGS and value is not None:
                    self._data["settings"][key] = bool(value)
                    changed = True
                    self.logs.info(f"{labels[key]} {'ON' if value else 'OFF'}")
            if changed:
                self.save()
            return dict(self._data["settings"])

    # ------------------------------------------------------- integrations
    def integration(self, agent_id: str) -> Dict[str, Any]:
        with self._lock:
            raw = self._data.setdefault("integrations", {}).get(agent_id)
            return dict(raw) if isinstance(raw, dict) else {}

    def set_integration(self, agent_id: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._data.setdefault("integrations", {})[agent_id] = {
                "enabled": bool(value.get("enabled")),
                "features": {k: bool(v) for k, v in (value.get("features") or {}).items()},
            }
            self.save()

    # -------------------------------------------------- discussion owners
    def discussion_owner(self, provider: str) -> str:
        """Agent id that owns the web discussion of this provider ('' = none)."""
        with self._lock:
            owner = self._data.get("discussion_owners", {}).get(provider)
            return owner if isinstance(owner, str) else ""

    def set_discussion_owner(self, provider: str, agent_id: str) -> None:
        with self._lock:
            owners = self._data.setdefault("discussion_owners", {})
            if agent_id:
                if owners.get(provider) == agent_id:
                    return
                owners[provider] = agent_id
            else:
                if provider not in owners:
                    return
                owners.pop(provider, None)
            self.save()

    def clear_discussion_owners(self) -> None:
        with self._lock:
            if not self._data.get("discussion_owners"):
                return
            self._data["discussion_owners"] = {}
            self.save()

    # ------------------------------------------------------------ api keys
    @property
    def auth_enabled(self) -> bool:
        with self._lock:
            return bool(self._data["api_keys"])

    @property
    def key_count(self) -> int:
        with self._lock:
            return len(self._data["api_keys"])

    def validate_key(self, provided: Optional[str]) -> bool:
        with self._lock:
            keys = self._data["api_keys"]
            if not keys:
                return True
            if not provided:
                return False
            digest = hash_api_key(provided)
            return any(hmac.compare_digest(digest, h) for h in keys)

    def list_keys(self, include_secret: bool = True) -> List[Dict[str, str]]:
        with self._lock:
            out = []
            for key_hash, meta in self._data["api_keys"].items():
                item = {
                    "id": meta.get("id", key_hash[:8]),
                    "name": meta.get("name", ""),
                    "created_at": meta.get("created_at", ""),
                }
                if include_secret:
                    item["key"] = meta.get("key", "")
                out.append(item)
            return out

    def create_key(self, name: str = "") -> Dict[str, str]:
        new_key = generate_api_key()
        key_hash = hash_api_key(new_key)
        with self._lock:
            self._data["api_keys"][key_hash] = {
                "id": key_hash[:8],
                "key": new_key,
                "name": name,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save()
        self.logs.info(f"Nouvelle clé API créée: {name or key_hash[:8]}")
        return {"api_key": new_key, "key_id": key_hash[:8]}

    def delete_key(self, key_id: str) -> bool:
        with self._lock:
            target = next(
                (h for h, meta in self._data["api_keys"].items() if meta.get("id", h[:8]) == key_id),
                None,
            )
            if not target:
                return False
            del self._data["api_keys"][target]
            self.save()
        self.logs.info(f"Clé API supprimée: {key_id}")
        return True

    def enable_protection(self) -> Dict[str, Any]:
        """Enable auth. Restores the backed-up keys if any, else creates a default key."""
        with self._lock:
            if self._data["api_keys"]:
                self.logs.info("Protection API déjà active")
                return {"ok": True, "message": "Protection déjà active"}
            backup = self._data.get("_api_keys_backup")
            if isinstance(backup, dict) and backup:
                self._data["api_keys"] = backup
                self._data.pop("_api_keys_backup", None)
                self.save()
                self.logs.info("Protection API réactivée (clés restaurées)")
                return {"ok": True, "message": "Protection restaurée", "key_count": len(backup)}
        created = self.create_key("default")
        self.logs.info("Protection API activée avec une clé par défaut")
        return {"ok": True, **created, "message": "Clé par défaut créée"}

    def disable_protection(self) -> Dict[str, Any]:
        with self._lock:
            if self._data["api_keys"]:
                self._data["_api_keys_backup"] = self._data["api_keys"]
            self._data["api_keys"] = {}
            self.save()
        self.logs.info("Protection API désactivée (les clés sont sauvegardées)")
        return {"ok": True, "message": "Protection désactivée"}

    def restore_protection(self) -> Optional[int]:
        with self._lock:
            backup = self._data.get("_api_keys_backup")
            if not backup:
                return None
            self._data["api_keys"] = backup
            self._data.pop("_api_keys_backup", None)
            self.save()
            count = len(backup)
        self.logs.info("Protection API restaurée depuis le backup")
        return count
