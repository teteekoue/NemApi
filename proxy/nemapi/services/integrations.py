"""AI-agent integrations (« Intégration agents IA » page of the web UI).

By default NemApi behaves like the classic NemApi 4.0: each request is typed in
full into the web chat (in a new discussion when « Nouvelle discussion » is
on) and the answer is returned. Nothing else happens.

Coding agents have their own context system (they resend their whole history,
send background requests, wait for very long answers…). An *integration*
adapts NemApi to one agent. It is off by default and only applies to the
requests of that agent, which are recognised on every request:
  * ``X-NemApi-Agent: <id>`` header (explicit, always wins);
  * the agent's User-Agent (Qwen Code sends ``QwenCode/<version> (…)``);
  * a marker of the agent's system prompt (fallback behind a rewriting proxy).
Requests from other clients keep the classic behaviour.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config_store import ConfigStore
from .logs import LogBuffer


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    description: str
    default: bool = True


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    vendor: str
    description: str
    setup: str  # how to point the agent to NemApi (shown in the UI)
    features: Tuple[Feature, ...]
    user_agents: Tuple[str, ...] = ()  # User-Agent prefixes (case-insensitive)
    prompt_markers: Tuple[str, ...] = ()  # substrings of the agent's system prompt
    homepage: str = ""

    def detect(self, headers: Mapping[str, str], messages: Sequence[Any]) -> Optional[str]:
        """How this request was recognised as coming from the agent, or None."""
        explicit = (headers.get("x-nemapi-agent") or "").strip().lower()
        if explicit:
            return "en-tête X-NemApi-Agent" if explicit == self.id else None
        ua = (headers.get("user-agent") or "").strip().lower()
        if any(ua.startswith(p.lower()) for p in self.user_agents):
            return "User-Agent"
        if self.prompt_markers:
            for m in messages[:3]:
                if isinstance(m, dict) and m.get("role") in ("system", "developer"):
                    text = _text(m.get("content"))
                    if any(marker in text for marker in self.prompt_markers):
                        return "prompt système"
        return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


QWEN_CODE = AgentProfile(
    id="qwen-code",
    name="Qwen Code",
    vendor="Alibaba · QwenLM",
    homepage="https://github.com/QwenLM/qwen-code",
    description=(
        "Agent de code en ligne de commande. Il renvoie tout son historique à chaque requête, envoie des "
        "requêtes d'arrière-plan (mémoire, suggestions, classifieur…) et écrit de gros fichiers en une "
        "seule réponse."
    ),
    setup=(
        "Dans ~/.qwen/settings.json (ou les variables OPENAI_*) : baseUrl http://127.0.0.1:8090/v1, "
        "modèle deepseek-chat (ou un autre modèle NemApi), clé API NemApi si la protection est active."
    ),
    user_agents=("QwenCode/",),
    prompt_markers=("You are Qwen Code",),
    features=(
        Feature(
            "incremental_context",
            "Contexte incrémental",
            "Une seule discussion web, jamais de nouveau chat : la 1re requête envoie tout (prompt système, "
            "outils, message), ensuite seuls les messages absents de la discussion sont tapés. Les doublons "
            "sont filtrés.",
        ),
        Feature(
            "side_queries_local",
            "Requêtes annexes en local",
            "Les requêtes d'arrière-plan de l'agent (sous-agent mémoire, suggestions de saisie, requêtes "
            "structurées respond_in_schema) reçoivent une réponse neutre immédiate, sans rien taper. Le "
            "classifieur de sécurité est refusé : l'agent te demande alors ton approbation.",
        ),
        Feature(
            "continuation",
            "Suite automatique des réponses coupées",
            "Si la réponse s'arrête au milieu d'un appel d'outil (gros fichier), NemApi demande la suite dans "
            "la même discussion et recolle les morceaux avant de répondre à l'agent.",
        ),
        Feature(
            "keepalive",
            "Connexion maintenue pendant les longues réponses",
            "En streaming, l'agent reçoit tout de suite les en-têtes puis un signal toutes les 15 s : il "
            "n'abandonne pas une réponse qui prend plusieurs minutes.",
        ),
    ),
)

AGENTS: Tuple[AgentProfile, ...] = (QWEN_CODE,)
AGENTS_BY_ID: Dict[str, AgentProfile] = {a.id: a for a in AGENTS}


@dataclass
class ActiveIntegration:
    """Integration applied to one request."""

    agent: AgentProfile
    features: Dict[str, bool]
    reason: str

    def has(self, feature: str) -> bool:
        return bool(self.features.get(feature))


@dataclass
class _Client:
    name: str
    agent: Optional[str]
    mode: str
    requests: int = 0
    last_seen: float = field(default_factory=time.time)


def client_name(user_agent: str) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "client inconnu"
    if ua.startswith("Mozilla/"):
        return "navigateur (page Chat test)"
    return ua.split(" ")[0][:60]


class IntegrationManager:
    """Per-agent switches (persisted in config.json) + request detection."""

    MAX_CLIENTS = 8

    def __init__(self, config: ConfigStore, logs: LogBuffer):
        self.config = config
        self.logs = logs
        self._lock = threading.Lock()
        self._clients: Dict[str, _Client] = {}
        self._seen: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ settings
    def state(self, agent_id: str) -> Dict[str, Any]:
        agent = AGENTS_BY_ID[agent_id]
        raw = self.config.integration(agent_id)
        feats = raw.get("features") if isinstance(raw.get("features"), dict) else {}
        return {
            "enabled": bool(raw.get("enabled", False)),  # off by default, for every agent
            "features": {f.id: bool(feats.get(f.id, f.default)) for f in agent.features},
        }

    def update(self, agent_id: str, enabled: Optional[bool] = None, features: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        agent = AGENTS_BY_ID[agent_id]
        current = self.state(agent_id)
        new = {"enabled": current["enabled"], "features": dict(current["features"])}
        if enabled is not None:
            new["enabled"] = bool(enabled)
        for fid, value in (features or {}).items():
            if fid in new["features"] and value is not None:
                new["features"][fid] = bool(value)
        self.config.set_integration(agent_id, new)
        if new["enabled"] != current["enabled"]:
            self.logs.info(
                f"Intégration {agent.name} {'activée' if new['enabled'] else 'désactivée'}"
                + ("" if new["enabled"] else " → comportement classique de NemApi")
            )
        labels = {f.id: f.label for f in agent.features}
        for fid, value in new["features"].items():
            if value != current["features"][fid]:
                self.logs.info(f"{agent.name} · {labels[fid]} {'ON' if value else 'OFF'}")
        return new

    def incremental_enabled(self) -> bool:
        return any(self.state(a.id)["enabled"] and self.state(a.id)["features"].get("incremental_context") for a in AGENTS)

    # ------------------------------------------------------------ requests
    def resolve(self, headers: Mapping[str, str], messages: Sequence[Any]) -> Optional[ActiveIntegration]:
        """Integration to apply to this request (None = classic NemApi)."""
        found: Optional[Tuple[AgentProfile, str]] = None
        for agent in AGENTS:
            reason = agent.detect(headers, messages)
            if reason:
                found = (agent, reason)
                break
        active = None
        if found is not None:
            agent, reason = found
            st = self.state(agent.id)
            with self._lock:
                seen = self._seen.setdefault(agent.id, {"requests": 0, "first_seen": time.time()})
                seen.update(requests=seen["requests"] + 1, last_seen=time.time(), reason=reason,
                            user_agent=(headers.get("user-agent") or "")[:120])
            if st["enabled"]:
                active = ActiveIntegration(agent, st["features"], reason)
        self._track(headers, found[0] if found else None, active)
        return active

    def _track(self, headers: Mapping[str, str], agent: Optional[AgentProfile], active: Optional[ActiveIntegration]) -> None:
        name = client_name(headers.get("user-agent") or "")
        mode = f"intégration {agent.name}" if active else (
            f"{agent.name} · intégration désactivée" if agent else "classique")
        key = f"{name}|{agent.id if agent else ''}"
        with self._lock:
            c = self._clients.get(key)
            if c is None:
                c = self._clients[key] = _Client(name=name, agent=agent.id if agent else None, mode=mode)
                if len(self._clients) > self.MAX_CLIENTS:
                    oldest = min(self._clients, key=lambda k: self._clients[k].last_seen)
                    self._clients.pop(oldest, None)
            c.mode = mode
            c.requests += 1
            c.last_seen = time.time()

    # ------------------------------------------------------------ views
    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        agents: List[Dict[str, Any]] = []
        for a in AGENTS:
            st = self.state(a.id)
            with self._lock:
                seen = dict(self._seen.get(a.id) or {})
            agents.append({
                "id": a.id,
                "name": a.name,
                "vendor": a.vendor,
                "homepage": a.homepage,
                "description": a.description,
                "setup": a.setup,
                "enabled": st["enabled"],
                "features": [
                    {"id": f.id, "label": f.label, "description": f.description, "default": f.default,
                     "enabled": st["features"][f.id]}
                    for f in a.features
                ],
                "detected": {
                    "requests": seen.get("requests", 0),
                    "seconds_ago": int(now - seen["last_seen"]) if seen.get("last_seen") else None,
                    "reason": seen.get("reason"),
                    "user_agent": seen.get("user_agent"),
                },
            })
        with self._lock:
            clients = sorted(self._clients.values(), key=lambda c: -c.last_seen)
            client_list = [
                {"name": c.name, "agent": c.agent, "mode": c.mode, "requests": c.requests,
                 "seconds_ago": int(now - c.last_seen)}
                for c in clients
            ]
        return {"agents": agents, "clients": client_list}

    def summary(self) -> Dict[str, bool]:
        return {a.id: self.state(a.id)["enabled"] for a in AGENTS}
