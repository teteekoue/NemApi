#!/usr/bin/env python3
"""NemApi v3.0 – Local OpenAI-compatible bridge with Firefox extension automation.

Simplified architecture:
- Receives a request
- Sends it to the selected provider
- Returns the response
- No incremental context, no filtering

Each completion must explicitly name both a provider and a model.
This prevents a request from being accidentally sent to another open AI tab.
"""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import socket
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from tools_format import (
    clean_assistant_text,
    format_messages_prompt,
    parse_tool_calls,
)

# Configuration du serveur - écoute sur toutes les interfaces réseau
HOST, PORT = "0.0.0.0", 8080

# Canonical model per provider (what the web UI actually uses)
PROVIDERS = {
    "deepseek": {"models": ["deepseek-chat"], "display_name": "DeepSeek"},
    "qwen": {"models": ["qwen-chat"], "display_name": "Qwen"},
    "claude": {"models": ["claude-chat"], "display_name": "Claude"},
    "gemini": {"models": ["gemini-chat"], "display_name": "Gemini"},
}

# Aliases accepted from clients (Qwen Code, Cursor, Continue, etc.)
# All map to the canonical provider; the web UI model stays whatever is selected there.
MODEL_ALIASES: Dict[str, str] = {
    # DeepSeek
    "deepseek-chat": "deepseek",
    "deepseek-coder": "deepseek",
    "deepseek-v3": "deepseek",
    "deepseek-r1": "deepseek",
    "chat": "deepseek",
    # Qwen
    "qwen-chat": "qwen",
    "qwen-plus": "qwen",
    "qwen2.5-plus": "qwen",
    "qwen2.5-coder": "qwen",
    "qwen3-coder": "qwen",
    "qwen3-coder-plus": "qwen",
    "qwen-max": "qwen",
    "plus": "qwen",
    # Claude
    "claude-chat": "claude",
    "claude-sonnet": "claude",
    "claude-3-sonnet": "claude",
    "claude-3-haiku": "claude",
    "claude-3.5-sonnet": "claude",
    "claude-4-sonnet": "claude",
    "sonnet": "claude",
    "haiku": "claude",
    # Gemini
    "gemini-chat": "gemini",
    "gemini-2.5-flash": "gemini",
    "gemini-2.0-flash": "gemini",
    "gemini-pro": "gemini",
    "gemini-flash": "gemini",
    "flash": "gemini",
}

# Model to provider mapping (canonical + provider/model form + aliases)
MODEL_TO_PROVIDER: Dict[str, str] = {}
for provider, info in PROVIDERS.items():
    for model in info["models"]:
        MODEL_TO_PROVIDER[model] = provider
        MODEL_TO_PROVIDER[f"{provider}/{model}"] = provider
for alias, provider in MODEL_ALIASES.items():
    MODEL_TO_PROVIDER[alias] = provider
    MODEL_TO_PROVIDER[f"{provider}/{alias}"] = provider

# Pricing per 1K tokens (approximate, for analytics)
TOKEN_PRICING = {
    "deepseek": {"prompt": 0.00067, "completion": 0.00083},
    "qwen": {"prompt": 0.0025, "completion": 0.0025},
    "claude": {"prompt": 0.003, "completion": 0.015},
    "gemini": {"prompt": 0.0025, "completion": 0.005},
}
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


# Fichier de configuration pour la persistance
CONFIG_FILE = Path(__file__).with_name("config.json")


def generate_api_key():
    """Génère une clé API sécurisée au format nemapi-token{random}"""
    random_part = secrets.token_urlsafe(24)
    return f"nemapi-token{random_part}"


def hash_api_key(key: str) -> str:
    """Hache une clé API pour stockage sécurisé"""
    return hashlib.sha256(key.encode()).hexdigest()


def load_config() -> Dict[str, Any]:
    """Charge la configuration depuis le fichier"""
    if not CONFIG_FILE.exists():
        return {
            "api_keys": {},
            "settings": {
                "stream_enabled": True,
                "auto_config": True,
                "fresh_chat": True,
                "premium_md": True,
            }
        }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        add_log(f"Erreur de chargement de la config: {e}", "error")
        return {
            "api_keys": {},
            "settings": {
                "stream_enabled": True,
                "auto_config": True,
                "fresh_chat": True,
                "premium_md": True,
            }
        }


def save_config(config: Dict[str, Any]):
    """Sauvegarde la configuration dans le fichier"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        add_log("Configuration sauvegardée")
    except Exception as e:
        add_log(f"Erreur de sauvegarde de la config: {e}", "error")


def validate_api_key(provided_key: str, config: Dict[str, Any]) -> bool:
    """Valide une clé API fournie"""
    # La protection est active si des clés sont configurées
    if not config.get("api_keys"):
        return True  # Pas de protection si aucune clé n'est configurée
    
    # Vérifier si la clé est dans le dictionnaire (clé: hash, valeur: métadonnées)
    key_hash = hash_api_key(provided_key)
    return key_hash in config["api_keys"]


def get_local_ip():
    """Récupère l'adresse IP locale pour l'affichage"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"


def calculate_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate estimated cost based on token usage."""
    if provider not in TOKEN_PRICING:
        return 0.0
    pricing = TOKEN_PRICING[provider]
    return (prompt_tokens / 1000) * pricing["prompt"] + (completion_tokens / 1000) * pricing["completion"]


class Job:
    def __init__(self, question: str, provider: str, model: str):
        self.id = str(uuid.uuid4())
        self.question = question
        self.provider = provider
        self.model = model
        self.status = "pending"
        self.result = ""
        self.error = ""
        self.event = threading.Event()
        self.start_time = time.time()


class Coordinator:
    def __init__(self):
        self.lock = threading.Lock()
        # Files d'attente par provider pour permettre le traitement parallèle
        self.queues: Dict[str, List[Job]] = {}
        self.jobs: Dict[str, Job] = {}
        # Suivi des jobs en cours par provider
        self.current_jobs: Dict[str, Optional[Job]] = {}

    def create(self, question: str, provider: str, model: str) -> Job:
        """Enqueue a job. Reuse an in-flight identical job (same provider+question)
        created < 8s ago so client retries do not double-fire the browser UI."""
        with self.lock:
            now = time.time()
            for existing in self.jobs.values():
                if existing.status not in ("pending", "dispatched"):
                    continue
                if existing.provider != provider:
                    continue
                if existing.question != question:
                    continue
                if now - existing.start_time > 8.0:
                    continue
                add_log(f"Dedup: reuse job {existing.id[:8]} (identical in-flight request)")
                return existing
            job = Job(question, provider, model)
            self.jobs[job.id] = job
            
            # Initialiser la file pour ce provider si elle n'existe pas
            if provider not in self.queues:
                self.queues[provider] = []
            self.queues[provider].append(job)
            
            return job

    def take(self) -> Optional[Job]:
        """Prend le prochain job disponible. 
        Priorité : un job pour un provider qui n'a pas de job en cours.
        Si tous les providers ont des jobs en cours, attend le premier disponible.
        """
        with self.lock:
            # D'abord, essayer de trouver un provider sans job en cours
            for provider, queue in self.queues.items():
                if queue and self.current_jobs.get(provider) is None:
                    job = queue.pop(0)
                    if job.status == "pending":
                        job.status = "dispatched"
                        self.current_jobs[provider] = job
                        return job
            
            # Si tous les providers sont occupés, vérifier s'il y a des jobs en attente
            # pour des providers qui ont déjà un job en cours
            for provider, queue in self.queues.items():
                if queue:
                    # Vérifier si le job en cours pour ce provider est toujours actif
                    current = self.current_jobs.get(provider)
                    if current is None:
                        job = queue.pop(0)
                        if job.status == "pending":
                            job.status = "dispatched"
                            self.current_jobs[provider] = job
                            return job
            
            return None

    def complete(self, job_id: str, action: str, result: str = "", error: str = ""):
        if not job_id:
            return
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ("pending", "dispatched"):
                return
            if action == "result":
                job.status, job.result = "done", result or ""
            elif action == "cancelled":
                job.status, job.error = "cancelled", error or "cancelled"
            else:
                job.status, job.error = "error", error or "automation failed"
            job.event.set()
            
            # Libérer le slot pour ce provider
            if job.provider in self.current_jobs and self.current_jobs[job.provider] and self.current_jobs[job.provider].id == job_id:
                self.current_jobs[job.provider] = None
            
            # Bound memory: drop oldest finished jobs beyond 200
            if len(self.jobs) > 250:
                finished = [
                    jid for jid, j in self.jobs.items()
                    if j.status in ("done", "error", "cancelled")
                ]
                for jid in finished[: len(finished) - 150]:
                    self.jobs.pop(jid, None)


coord = Coordinator()
start_time = time.time()

# Charger la configuration au démarrage
app_config = load_config()

# Enhanced stats with per-provider tracking
stats = {
    "requests": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "estimated_cost": 0.0,
    "providers": {
        "deepseek": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
        "qwen": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
        "claude": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
        "gemini": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
    }
}
stats_lock = threading.Lock()

# Charger les paramètres depuis la configuration
stream_enabled = app_config["settings"].get("stream_enabled", True)
fresh_chat_enabled = app_config["settings"].get("fresh_chat", True)
premium_md_enabled = app_config["settings"].get("premium_md", True)

# State for auto-configuration
ext_lock = threading.RLock()
ext_state = {
    "tabs": [],
    "targetTabs": {},
    "connected": False,
    "busy": False,
    "lastSeen": 0.0,
    "logs": [],
    "autoConfig": app_config["settings"].get("auto_config", True),
}

# Verrou pour la configuration
config_lock = threading.RLock()


def add_log(message: str, level: str = "info"):
    with ext_lock:
        ext_state["logs"].append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": message})
        ext_state["logs"] = ext_state["logs"][-120:]
    print(f"[{level.upper()}] {message}", flush=True)


def approx_tokens(value: str) -> int:
    return max(1, len(value or "") // 4)


def extension_connected() -> bool:
    with ext_lock:
        return bool(ext_state["connected"] and time.time() - ext_state["lastSeen"] < 8)


def read_page(name: str) -> str:
    try:
        return (Path(__file__).with_name(name)).read_text(encoding="utf-8")
    except OSError as exc:
        return f"<h1>NemApi</h1><pre>Page unavailable: {exc}</pre>"


class Handler(BaseHTTPRequestHandler):
    server_version = "NemApi/2.0"

    def log_message(self, _fmt, *_args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, obj: Any, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client a fermé la connexion, ignorer l'erreur
            pass

    def _html(self, value: str):
        data = value.encode("utf-8")
        try:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client a fermé la connexion, ignorer l'erreur
            pass

    def _get_api_key(self) -> Optional[str]:
        """Extrait la clé API de l'en-tête Authorization"""
        auth_header = self.headers.get("Authorization")
        if not auth_header:
            return None
        
        # Support des formats: Bearer <key> ou ApiKey <key>
        if auth_header.startswith("Bearer ") or auth_header.startswith("ApiKey "):
            return auth_header.split(" ", 1)[1]
        return None

    def _validate_api_key(self) -> bool:
        """Valide la clé API pour les endpoints protégés"""
        api_key = self._get_api_key()
        if not api_key:
            return False
        return validate_api_key(api_key, app_config)

    def _read_json(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0:
            return {}
        value = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._html(read_page("admin.html"))
        elif path == "/config":
            self._html(read_page("config.html"))
        elif path == "/chat":
            self._html(read_page("chat.html"))
        elif path == "/analytics":
            self._html(read_page("analytics.html"))
        elif path == "/api-keys-page":
            self._html(read_page("api-keys.html"))
        elif path == "/status":
            self._json({"status": "ready" if extension_connected() else "waiting_extension", "extension": extension_connected()})
        elif path == "/stats":
            with stats_lock:
                self._json({
                    "uptime_seconds": int(time.time() - start_time),
                    "total_requests": stats["requests"],
                    "prompt_tokens": stats["prompt_tokens"],
                    "completion_tokens": stats["completion_tokens"],
                    "estimated_cost": round(stats["estimated_cost"], 4),
                    "providers": stats["providers"]
                })
        elif path == "/settings":
            with ext_lock:
                self._json({
                    "stream_enabled": stream_enabled,
                    "auto_config": ext_state.get("autoConfig", True),
                    "fresh_chat": fresh_chat_enabled,
                    "premium_md": premium_md_enabled,
                })
        elif path == "/api-keys/config":
            # Retourne la configuration complète des clés API
            with config_lock:
                self._json({
                    "enabled": bool(app_config["api_keys"]),
                    "require_auth": bool(app_config["api_keys"]),
                    "key_count": len(app_config["api_keys"])
                })
        elif path == "/providers":
            self._json({
                "providers": [
                    {
                        "id": key,
                        "models": info["models"],
                        "display_name": info.get("display_name", key),
                    }
                    for key, info in PROVIDERS.items()
                ]
            })
        elif path == "/api-keys":
            # Retourne la liste des clés API avec les clés complètes
            with config_lock:
                api_keys_info = []
                for key_hash, metadata in app_config["api_keys"].items():
                    api_keys_info.append({
                        "id": metadata.get("id", key_hash[:8]),
                        "key": metadata.get("key", ""),
                        "name": metadata.get("name", ""),
                        "created_at": metadata.get("created_at", "")
                    })
                self._json({"api_keys": api_keys_info, "require_auth": bool(app_config["api_keys"])})
        elif path == "/v1/models":
            # OpenAI-compatible: plain ids + provider/model + popular aliases
            # so Qwen Code / Cursor can select any of these model names.
            data = []
            seen = set()
            for provider, info in PROVIDERS.items():
                for model in info["models"]:
                    for mid in (model, f"{provider}/{model}"):
                        if mid in seen:
                            continue
                        seen.add(mid)
                        data.append({
                            "id": mid,
                            "object": "model",
                            "created": int(start_time),
                            "owned_by": provider,
                        })
            for alias, provider in MODEL_ALIASES.items():
                if alias in seen or provider not in PROVIDERS:
                    continue
                seen.add(alias)
                data.append({
                    "id": alias,
                    "object": "model",
                    "created": int(start_time),
                    "owned_by": provider,
                })
            self._json({"object": "list", "data": data})
        elif path == "/job":
            job = coord.take()
            if job:
                self._json({
                    "action": "ask",
                    "jobId": job.id,
                    "question": job.question,
                    "provider": job.provider,
                    "model": job.model,
                    "freshChat": fresh_chat_enabled,
                    "premiumMd": premium_md_enabled,
                })
            else:
                self._json({"action": "idle"})
        elif path == "/extension/state":
            with ext_lock:
                state = {**ext_state, "targetTabs": dict(ext_state["targetTabs"]), "logs": list(ext_state["logs"]), "connected": extension_connected()}
            self._json(state)
        elif path == "/config-full":
            # Retourne la configuration complète (pour l'interface d'administration)
            with config_lock:
                self._json({
                    "api_keys": {
                        "enabled": bool(app_config["api_keys"]),
                        "count": len(app_config["api_keys"]),
                        "keys": [
                            {
                                "id": metadata.get("id", key_hash[:8]),
                                "name": metadata.get("name", ""),
                                "created_at": metadata.get("created_at", "")
                            }
                            for key_hash, metadata in app_config["api_keys"].items()
                        ]
                    },
                    "settings": app_config["settings"]
                })
        elif path == "/extension/config":
            with ext_lock:
                self._json({
                    "targetTabs": dict(ext_state["targetTabs"]),
                    "autoConfig": ext_state.get("autoConfig", True),
                    "freshChat": fresh_chat_enabled,
                    "premiumMd": premium_md_enabled,
                })
        elif self._serve_static(path):
            return
        else:
            self._json({"error": {"message": "not found", "type": "not_found"}}, 404)

    def _serve_static(self, path: str) -> bool:
        rel = path.lstrip("/")
        if not rel or ".." in rel:
            return False
        base = Path(__file__).parent.resolve()
        target = (base / rel).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            return False
        ctype = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        try:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            # Client a fermé la connexion, ignorer l'erreur
            return False

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as exc:
            self._json({"error": {"message": f"Invalid JSON: {exc}", "type": "invalid_request"}}, 400)
            return

        if path == "/job":
            job_id, action = body.get("jobId"), body.get("action")
            coord.complete(job_id, action or "error", result=body.get("result") or "", error=body.get("error") or "")
            level = "info" if action == "result" else "error"
            detail = f"{len(body.get('result') or '')} chars" if action == "result" else (body.get("error") or "unknown error")
            add_log(f"Job {str(job_id)[:8]} {'OK' if action == 'result' else action}: {detail}", level)
            self._json({"ok": True})
        elif path == "/extension/log":
            level = body.get("level") if body.get("level") in ("info", "warn", "error") else "info"
            add_log(f"Extension: {str(body.get('message') or '')[:500]}", level)
            self._json({"ok": True})
        elif path == "/extension/tabs":
            with ext_lock:
                ext_state["tabs"] = body.get("tabs") or []
                ext_state["connected"] = True
                ext_state["busy"] = bool(body.get("busy"))
                ext_state["lastSeen"] = time.time()
                
                # Auto-configuration: automatically select first available tab for each provider
                if ext_state.get("autoConfig", True):
                    for provider in PROVIDERS.keys():
                        if provider not in ext_state["targetTabs"]:
                            # Find first tab for this provider
                            for tab in ext_state["tabs"]:
                                if tab.get("provider") == provider:
                                    ext_state["targetTabs"][provider] = tab.get("id")
                                    add_log(f"Auto-configured {provider} tab: {tab.get('id')}")
                                    break
            self._json({"ok": True})
        elif path == "/extension/config":
            provider, tab_id = body.get("provider"), body.get("targetTabId")
            if provider not in PROVIDERS or not isinstance(tab_id, int):
                self._json({"error": {"message": "provider and numeric targetTabId are required", "type": "invalid_request"}}, 400)
                return
            with ext_lock:
                matching = next((tab for tab in ext_state["tabs"] if tab.get("id") == tab_id and tab.get("provider") == provider), None)
                if not matching:
                    self._json({"error": {"message": f"Tab {tab_id} is not an open {provider} tab", "type": "invalid_request"}}, 400)
                    return
                ext_state["targetTabs"][provider] = tab_id
            add_log(f"Selected {provider} tab: {tab_id}")
            self._json({"ok": True, "targetTabs": ext_state["targetTabs"]})
        elif path == "/settings":
            global stream_enabled, fresh_chat_enabled, premium_md_enabled
            settings_updated = False
            
            if "stream_enabled" in body:
                stream_enabled = bool(body["stream_enabled"])
                app_config["settings"]["stream_enabled"] = stream_enabled
                settings_updated = True
                add_log(f"Stream {'ON' if stream_enabled else 'OFF'}")
            if "auto_config" in body:
                with ext_lock:
                    ext_state["autoConfig"] = bool(body["auto_config"])
                    app_config["settings"]["auto_config"] = ext_state["autoConfig"]
                settings_updated = True
                add_log(f"Auto-config {'ON' if ext_state['autoConfig'] else 'OFF'}")
            if "fresh_chat" in body:
                fresh_chat_enabled = bool(body["fresh_chat"])
                app_config["settings"]["fresh_chat"] = fresh_chat_enabled
                settings_updated = True
                add_log(f"Fresh-chat {'ON' if fresh_chat_enabled else 'OFF'}")
            if "premium_md" in body:
                premium_md_enabled = bool(body["premium_md"])
                app_config["settings"]["premium_md"] = premium_md_enabled
                settings_updated = True
                add_log(f"Premium-MD {'ON' if premium_md_enabled else 'OFF'}")
            
            if settings_updated:
                save_config(app_config)
            
            self._json({
                "stream_enabled": stream_enabled,
                "auto_config": ext_state.get("autoConfig", True),
                "fresh_chat": fresh_chat_enabled,
                "premium_md": premium_md_enabled,
            })
        elif path == "/stop":
            with coord.lock:
                for job in coord.jobs.values():
                    if job.status in ("pending", "dispatched"):
                        job.status, job.error = "cancelled", "stopped"
                        job.event.set()
                coord.queue.clear()
                coord.current = None
            add_log("All pending jobs stopped", "warn")
            self._json({"ok": True})
        elif path == "/api-keys/enable":
            # Active la protection par clé API
            with config_lock:
                # Si aucune clé n'existe, en créer une par défaut
                if not app_config["api_keys"]:
                    new_key = generate_api_key()
                    key_hash = hash_api_key(new_key)
                    app_config["api_keys"][key_hash] = {
                        "id": key_hash[:8],
                        "key": new_key,
                        "name": "default",
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    save_config(app_config)
                    add_log("Protection API activée avec une clé par défaut")
                    self._json({"ok": True, "api_key": new_key, "key_id": key_hash[:8], "message": "Clé par défaut créée"})
                else:
                    add_log("Protection API déjà active")
                    self._json({"ok": True, "message": "Protection déjà active"})
        elif path == "/api-keys/disable":
            # Désactive la protection par clé API en vidant le dictionnaire
            # Note: les clés ne sont pas supprimées du fichier, juste désactivées
            with config_lock:
                # Sauvegarder les clés actuelles dans un backup
                if app_config["api_keys"]:
                    app_config["_api_keys_backup"] = app_config["api_keys"]
                app_config["api_keys"] = {}
                save_config(app_config)
                add_log("Protection API désactivée (les clés sont sauvegardées)")
                self._json({"ok": True, "message": "Protection désactivée"})
        elif path == "/api-keys/restore":
            # Restaure les clés depuis le backup
            with config_lock:
                if app_config.get("_api_keys_backup"):
                    app_config["api_keys"] = app_config["_api_keys_backup"]
                    del app_config["_api_keys_backup"]
                    save_config(app_config)
                    add_log("Protection API restaurée depuis le backup")
                    self._json({"ok": True, "message": "Protection restaurée", "key_count": len(app_config["api_keys"])})
                else:
                    self._json({"error": {"message": "Aucun backup de clés trouvé", "type": "not_found"}}, 404)
        elif path == "/api-keys":
            # Créer une nouvelle clé API
            with config_lock:
                name = body.get("name", "")
                new_key = generate_api_key()
                key_hash = hash_api_key(new_key)
                app_config["api_keys"][key_hash] = {
                    "id": key_hash[:8],
                    "key": new_key,
                    "name": name,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                save_config(app_config)
                add_log(f"Nouvelle clé API créée: {name or key_hash[:8]}")
                self._json({"ok": True, "api_key": new_key, "key_id": key_hash[:8]})
        elif path.startswith("/api-keys/"):
            # Supprimer une clé API
            key_id = path.split("/")[2]
            with config_lock:
                # Trouver la clé par son ID
                key_to_delete = None
                for key_hash, metadata in app_config["api_keys"].items():
                    if metadata.get("id") == key_id:
                        key_to_delete = key_hash
                        break
                
                if key_to_delete:
                    del app_config["api_keys"][key_to_delete]
                    save_config(app_config)
                    add_log(f"Clé API supprimée: {key_id}")
                    self._json({"ok": True})
                else:
                    self._json({"error": {"message": "Clé API non trouvée", "type": "not_found"}}, 404)
        elif path == "/v1/chat/completions":
            # Vérifier la clé API si elle est requise
            if app_config["api_keys"] and not self._validate_api_key():
                self._json({
                    "error": {
                        "message": "Clé API requise. Ajoutez un en-tête Authorization: Bearer <votre_clé>",
                        "type": "unauthorized"
                    }
                }, 401)
                return
            self._create_completion(body)
        else:
            self._json({"error": {"message": "not found", "type": "not_found"}}, 404)

    def _create_completion(self, body: dict):
        model = str(body.get("model") or "").strip()
        explicit_provider = str(body.get("provider") or "").lower().strip()

        # Determine provider: explicit provider takes precedence, otherwise deduce from model
        if explicit_provider:
            provider = explicit_provider
        elif model:
            # Exact match first, then alias / provider prefix
            provider = MODEL_TO_PROVIDER.get(model)
            if provider is None and "/" in model:
                head = model.split("/")[0].lower().strip()
                tail = model.split("/")[-1]
                provider = MODEL_TO_PROVIDER.get(tail) or (head if head in PROVIDERS else None)
            if not provider or provider not in PROVIDERS:
                # Fuzzy: any alias containing the model token
                low = model.lower()
                for alias, prov in MODEL_ALIASES.items():
                    if alias in low or low in alias:
                        provider = prov
                        break
            if not provider or provider not in PROVIDERS:
                valid = sorted(set(list(MODEL_ALIASES.keys()) + [f"{p}/{i['models'][0]}" for p, i in PROVIDERS.items()]))
                self._json({
                    "error": {
                        "message": f"Unknown model '{model}'. Try one of: {', '.join(valid[:12])}…",
                        "type": "invalid_request",
                    }
                }, 400)
                return
        else:
            self._json({
                "error": {
                    "message": "The 'model' field is required. Examples: 'qwen-chat', 'qwen-plus', 'deepseek-chat', 'claude-sonnet'.",
                    "type": "invalid_request",
                }
            }, 400)
            return

        if provider not in PROVIDERS:
            self._json({
                "error": {
                    "message": f"Unknown provider '{provider}'. Available: {', '.join(PROVIDERS.keys())}.",
                    "type": "invalid_request",
                }
            }, 400)
            return

        # Echo the client-requested model id in the OpenAI response (Qwen Code
        # and similar clients may assert it matches what they sent). Internally
        # we only route by provider; the web UI model is whatever is selected there.
        response_model = model or PROVIDERS[provider]["models"][0]
        internal_model = PROVIDERS[provider]["models"][0]

        # Accept the request and wait briefly for the extension to come online
        # (reload, restart) instead of rejecting immediately with 503.
        if not extension_connected():
            wait_deadline = time.time() + 30
            while not extension_connected() and time.time() < wait_deadline:
                time.sleep(0.5)
            if not extension_connected():
                self._json({
                    "error": {
                        "message": "Firefox extension is not connected. Load it and open the provider tab.",
                        "type": "extension_unavailable",
                    }
                }, 503)
                return

        with ext_lock:
            if provider not in ext_state["targetTabs"]:
                self._json({
                    "error": {
                        "message": f"No {provider} tab is selected. Open http://127.0.0.1:8080/ to configure.",
                        "type": "provider_not_selected",
                    }
                }, 409)
                return

        messages = body.get("messages") or []
        if not messages:
            self._json({
                "error": {"message": "At least one message is required.", "type": "invalid_request"}
            }, 400)
            return

        # Full OpenAI-style tools (Qwen Code, Cursor, etc.)
        tools = body.get("tools") or body.get("functions")
        if tools and not isinstance(tools, list):
            tools = None

        # Build a clean, role-labelled prompt (handles system / tool results / tool_calls)
        prompt = format_messages_prompt(messages, tools=tools)
        if not prompt.strip():
            self._json({
                "error": {"message": "Messages produced an empty prompt.", "type": "invalid_request"}
            }, 400)
            return

        requested_tool_names: List[str] = []
        if tools:
            for t in tools:
                fn = (t or {}).get("function") or t or {}
                if fn.get("name"):
                    requested_tool_names.append(str(fn["name"]))

        # Respect explicit stream flag from client (Qwen Code always sends stream:true).
        # Fall back to admin toggle only when the field is omitted.
        if "stream" in body:
            stream_requested = bool(body.get("stream"))
        else:
            stream_requested = bool(stream_enabled)
        # Some SDKs only signal streaming via Accept header
        accept = (self.headers.get("Accept") or "").lower()
        if "text/event-stream" in accept:
            stream_requested = True

        add_log(
            f"Completion → {provider}/{response_model} stream={stream_requested} "
            f"tools={len(requested_tool_names)} prompt={len(prompt)}c"
        )
        self._handle_completion(
            prompt,
            provider,
            response_model,
            stream_requested,
            requested_tool_names=requested_tool_names or None,
            internal_model=internal_model,
        )

    def _handle_completion(
        self,
        prompt: str,
        provider: str,
        model: str,
        stream: bool,
        requested_tool_names: list | None = None,
        internal_model: str | None = None,
    ):
        job = coord.create(prompt, provider, internal_model or model)
        add_log(f"Job {job.id[:8]} queued → {provider}/{model} ({len(prompt)} chars)")
        if not job.event.wait(timeout=240):
            coord.complete(job.id, "error", error="timeout waiting for extension")
            add_log(f"Job {job.id[:8]} timed out", "error")
            self._json({"error": {"message": "Timeout waiting for the browser extension. Check the admin log.", "type": "timeout"}}, 504)
            return
        if job.status != "done":
            self._json({"error": {"message": job.error or "automation failed", "type": "automation_error"}}, 500)
            return

        # Robust post-processing of DOM-extracted text for coding agents
        text = clean_assistant_text(job.result or "")
        remaining, tool_calls = parse_tool_calls(text, requested_tool_names)
        if tool_calls:
            text = remaining
        finish_reason = "tool_calls" if tool_calls else "stop"

        prompt_tokens = approx_tokens(prompt)
        completion_tokens = approx_tokens(text)
        estimated_cost = calculate_cost(provider, prompt_tokens, completion_tokens)

        with stats_lock:
            stats["requests"] += 1
            stats["prompt_tokens"] += prompt_tokens
            stats["completion_tokens"] += completion_tokens
            stats["estimated_cost"] += estimated_cost
            if provider in stats["providers"]:
                stats["providers"][provider]["requests"] += 1
                stats["providers"][provider]["prompt_tokens"] += prompt_tokens
                stats["providers"][provider]["completion_tokens"] += completion_tokens
                stats["providers"][provider]["cost"] += estimated_cost

        chat_id = "chatcmpl-" + uuid.uuid4().hex[:10]

        # Qwen Code / OpenAI SDK: if the client asked for stream:true, the
        # response MUST be text/event-stream. Returning application/json causes:
        #   "Streaming request received a non-SSE response"
        if stream:
            self._sse(chat_id, provider, model, text, tool_calls=tool_calls or None)
        else:
            message: Dict[str, Any] = {
                "role": "assistant",
                "content": text if text else (None if tool_calls else ""),
            }
            if tool_calls:
                message["tool_calls"] = tool_calls
            self._json({
                "id": chat_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            })

    def _sse(
        self,
        chat_id: str,
        provider: str,
        model: str,
        text: str,
        tool_calls: list | None = None,
    ):
        """OpenAI-compatible SSE stream. Always used when client requests stream:true."""
        try:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(value: dict):
                payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()

            created = int(time.time())
            common = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
            }

            # 1) role chunk
            emit({
                **common,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })

            # 2) text content (if any)
            if text:
                chunk_size = 64
                for offset in range(0, len(text), chunk_size):
                    piece = text[offset : offset + chunk_size]
                    emit({
                        **common,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": piece},
                            "finish_reason": None,
                        }],
                    })
                    time.sleep(0.001)

            # 3) tool_calls (OpenAI streaming shape) if present
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    fn = tc.get("function") or {}
                    # first delta: id + name + type
                    emit({
                        **common,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": i,
                                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                    "type": "function",
                                    "function": {
                                        "name": fn.get("name") or "",
                                        "arguments": "",
                                    },
                                }]
                            },
                            "finish_reason": None,
                        }],
                    })
                    # arguments in one or more chunks
                    args = fn.get("arguments") or "{}"
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    arg_chunk = 80
                    for offset in range(0, len(args), arg_chunk):
                        emit({
                            **common,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": i,
                                        "function": {"arguments": args[offset : offset + arg_chunk]},
                                    }]
                                },
                                "finish_reason": None,
                            }],
                        })

            finish = "tool_calls" if tool_calls else "stop"
            emit({
                **common,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            })

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

            try:
                import socket
                if hasattr(self, "request") and isinstance(self.request, socket.socket):
                    self.request.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def main():
    local_ip = get_local_ip()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"NemApi v3.0 listening on http://{HOST}:{PORT}/")
    print(f"Local network URL: http://{local_ip}:{PORT}/")
    print("Admin UI  → http://127.0.0.1:8080/")
    print("Analytics → http://127.0.0.1:8080/analytics")
    print("API       → POST /v1/chat/completions (provider + model required)")
    print("\nStandard models: deepseek-chat, qwen-chat, claude-chat, gemini-chat")
    print("Auto-configuration is enabled by default")
    print(f"\nAccessible from local network: http://{local_ip}:{PORT}/v1/chat/completions")
    
    # Afficher l'état de la protection API
    if app_config["api_keys"]:
        print(f"\n[SECURITY] API key protection is ENABLED ({len(app_config['api_keys'])} key(s) configured)")
        print("           Use 'Authorization: Bearer <your_key>' header for API requests")
    else:
        print("\n[SECURITY] API key protection is DISABLED - No keys configured")
        print("           Add API keys via admin UI or POST /api-keys endpoint")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
