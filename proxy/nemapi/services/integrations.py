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

from ..core.providers import PROVIDERS
from .config_store import ConfigStore
from .logs import LogBuffer


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    description: str
    default: bool = True
    tag: str = ""  # short chip shown on the agent card (what it brings)


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
    snippet: str = ""  # ready-to-copy configuration; {base} is the NemApi base URL
    # Header value the agent must send so NemApi recognises it on EVERY
    # request. The prompt marker is only good for the first turn (an agent
    # sends its system prompt once), so the header is the reliable path.
    header: str = ""
    # The agent never sends a "tools" array (it brings its own dialect), so
    # a request that carries one is another client's, not its.
    native_tools_only: bool = False

    def accepts_request(self, messages: Sequence[Any], tools: Optional[List[Any]]) -> bool:
        """Is this request compatible with what this agent sends?

        The owner of a discussion is used when a request carries no
        identifying signal. A request that carries something the agent would
        NEVER send belongs to another client and must not inherit its mode.
        """
        if self.native_tools_only and tools:
            return False
        return True

    def detect(self, headers: Mapping[str, str], messages: Sequence[Any]) -> Optional[str]:
        """How this request was recognised as coming from the agent, or None."""
        explicit = (headers.get("x-nemapi-agent") or "").strip().lower()
        if explicit:
            return "en-tête X-NemApi-Agent" if explicit == self.id else None
        ua = (headers.get("user-agent") or "").strip().lower()
        if any(ua.startswith(p.lower()) for p in self.user_agents):
            return "User-Agent"
        if self.prompt_markers:
            for i, m in enumerate(messages[:3]):
                if not isinstance(m, dict):
                    continue
                # An agent that merges its system prompt into the first user
                # message (Nemesis does) must still be recognised: the first
                # message is scanned whatever its role.
                if i and m.get("role") not in ("system", "developer"):
                    continue
                text = _text(m.get("content"))
                if any(marker in text for marker in self.prompt_markers):
                    return "prompt système" if m.get("role") in ("system", "developer") else "prompt de l'agent"
        return None

    def identifiers(self) -> List[Dict[str, str]]:
        """How this agent can be recognised, best method first."""
        out: List[Dict[str, str]] = []
        if self.header:
            out.append({
                "kind": "header",
                "value": f"X-NemApi-Agent: {self.header}",
                "note": "recommandé — recognised à chaque requête",
            })
        if self.user_agents:
            out.append({"kind": "user-agent", "value": self.user_agents[0] + "…", "note": "automatique"})
        if self.prompt_markers:
            out.append({"kind": "prompt", "value": self.prompt_markers[0], "note": "première requête seulement"})
        return out

    def owns_discussion(self) -> bool:
        """True when the agent never opens a new discussion (it continues the
        one already open), which is what makes it the owner of that web
        discussion."""
        return any(f.id == "persistent_chat" for f in self.features)


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
    header="qwen-code",
    snippet=(
        'export OPENAI_BASE_URL="{base}"\n'
        'export OPENAI_API_KEY="nemapi"       # ta clé NemApi si la protection est active\n'
        'export OPENAI_MODEL="deepseek-chat"  # ou qwen-chat, claude-chat, gemini-chat…\n'
        "# Qwen Code est reconnu par son User-Agent (QwenCode/…) : rien à ajouter.\n"
        "qwen"
    ),
    features=(
        Feature(
            "persistent_chat",
            "Plus de nouvelle URL",
            "La requête ne rouvre jamais le chat web : l'URL de la discussion reste la même d'un tour à "
            "l'autre. Sans cela, chaque requête repartirait d'une discussion vide — et l'agent devrait "
            "renvoyer tout son contexte à chaque fois.",
            tag="discussion",
        ),
        Feature(
            "incremental_context",
            "Contexte incrémental",
            "Le contexte n'est plus renvoyé par l'agent : NemApi compare son historique avec ce que la "
            "discussion contient déjà et ne tape que ce qui manque (prompt système, outils, message). "
            "Les doublons sont filtrés.",
            tag="contexte",
        ),
        Feature(
            "tool_calls",
            "Appels d'outils",
            "Le tableau « tools » OpenAI de l'agent est transmis au modèle avec son contrat d'appel, et la "
            "réponse est renvoyée au format OpenAI tool_calls. Les appels dont le nom est inconnu ne sont "
            "pas exécutés.",
            tag="outils",
        ),
        Feature(
            "side_queries_local",
            "Requêtes annexes en local",
            "Les requêtes d'arrière-plan de l'agent (sous-agent mémoire, suggestions de saisie, requêtes "
            "structurées respond_in_schema) reçoivent une réponse neutre immédiate, sans rien taper. Le "
            "classifieur de sécurité est refusé : l'agent te demande alors ton approbation.",
            tag="local",
        ),
        Feature(
            "continuation",
            "Suite automatique des réponses coupées",
            "Si la réponse s'arrête au milieu d'un appel d'outil (gros fichier), NemApi demande la suite dans "
            "la même discussion et recolle les morceaux avant de répondre à l'agent.",
            tag="fichiers longs",
        ),
        Feature(
            "keepalive",
            "Connexion maintenue pendant les longues réponses",
            "En streaming, l'agent reçoit tout de suite les en-têtes puis un signal toutes les 15 s : il "
            "n'abandonne pas une réponse qui prend plusieurs minutes.",
            tag="réseau",
        ),
    ),
)

NEMESIS = AgentProfile(
    id="nemesis",
    name="Nemesis",
    vendor="teteekoue · NEMESIS-AGENT",
    homepage="https://github.com/teteekoue/NEMESIS-AGENT",
    description=(
        "Agent de code en ligne de commande. Il n'envoie que le message courant (son contexte est "
        "géré de son côté) et ses appels d'outils sont écrits en JSON dans un bloc de code."
    ),
    setup=(
        "Dans le config.yaml de NEMESIS : section « nemapi » (host 127.0.0.1, port 8090, model "
        "deepseek-chat). NemApi reconnaît l'agent via l'en-tête X-NemApi-Agent: nemesis, ou via son "
        "prompt système."
    ),
    user_agents=("nemesis-agent", "nemesis-cli", "nemesis/"),
    # The canonical runtime prompt (NEMESIS-AGENT/src/core/system_prompt.py)
    # opens with « You are **NEMESIS** »; prompt_system.txt is the older
    # fallback wording.
    prompt_markers=("You are **NEMESIS**", "NEMESIS Runtime Prompt", "NEMESIS is a reliable engineering agent"),
    header="nemesis",
    native_tools_only=True,
    snippet=(
        "# config.yaml de NEMESIS\n"
        "provider:\n"
        "  type: nemapi\n"
        "  model: deepseek-chat\n"
        "\n"
        "nemapi:\n"
        '  url: "{base}"\n'
        "  # token: nemapi-token…   # si la protection API est active\n"
        "  # agent: nemesis        # facultatif : en-tête X-NemApi-Agent"
    ),
    features=(
        Feature(
            "persistent_chat",
            "Discussion unique",
            "La requête ne déclenche jamais l'ouverture d'une nouvelle discussion : l'URL du chat web "
            "reste la même et l'agent continue dans la discussion ouverte. C'est le pendant de "
            "« Nouvelle discussion à chaque requête » du mode classique.",
            tag="discussion",
        ),
        Feature(
            "native_tool_calls",
            "Appels d'outils natifs",
            "Les appels d'outils écrits par l'agent (JSON dans un bloc ```json) sont renvoyés au format "
            "OpenAI tool_calls au lieu de rester dans le texte. Le contrat d'outils de NemApi n'est pas "
            "réécrit par NemApi : c'est l'agent qui décide du format.",
            tag="outils",
        ),
    ),
)


AGENTS: Tuple[AgentProfile, ...] = (QWEN_CODE, NEMESIS)
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

    # ------------------------------------------------- discussion owners
    def owner_of(self, provider: str) -> str:
        """Agent that owns the web discussion of this provider ('' = none)."""
        if not provider:
            return ""
        owner = self.config.discussion_owner(provider)
        if not owner:
            return ""
        agent = AGENTS_BY_ID.get(owner)
        if agent is None or not self.state(owner)["enabled"]:
            return ""
        return owner

    def _remember_owner(self, provider: str, agent: AgentProfile, active: bool) -> None:
        """An agent that never opens a new discussion owns the one that is
        open: NemApi remembers it, so the requests that follow — which carry
        no identifying signal any more — stay in that agent's mode."""
        if not provider:
            return
        if active and agent.owns_discussion():
            self.config.set_discussion_owner(provider, agent.id)
        else:
            self.config.set_discussion_owner(provider, "")

    def forget_owners(self) -> None:
        self.config.clear_discussion_owners()

    # ------------------------------------------------------------ requests
    def resolve(self, headers: Mapping[str, str], messages: Sequence[Any],
                provider: str = "", tools: Optional[List[Any]] = None) -> Optional[ActiveIntegration]:
        """Integration to apply to this request (None = classic NemApi)."""
        found: Optional[Tuple[AgentProfile, str]] = None
        for agent in AGENTS:
            reason = agent.detect(headers, messages)
            if reason:
                found = (agent, reason)
                break
        if found is None and provider:
            # No signal this time: the discussion is still the one the agent
            # opened, so the request belongs to that agent.
            owner_id = self.owner_of(provider)
            if owner_id:
                owner = AGENTS_BY_ID.get(owner_id)
                if owner is not None and owner.accepts_request(messages, tools):
                    found = (owner, "discussion de l'agent")
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
            self._remember_owner(provider, agent, active is not None)
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
                "snippet": a.snippet,
                "header": a.header,
                "identifiers": a.identifiers(),
                "enabled": st["enabled"],
                "features": [
                    {"id": f.id, "label": f.label, "description": f.description, "default": f.default,
                     "enabled": st["features"][f.id], "tag": f.tag}
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
        owners = {pid: aid for pid in PROVIDERS if (aid := self.owner_of(pid))}
        return {"agents": agents, "clients": client_list, "discussion_owners": owners}

    def summary(self) -> Dict[str, bool]:
        return {a.id: self.state(a.id)["enabled"] for a in AGENTS}
