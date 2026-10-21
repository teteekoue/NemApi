"""Agent side queries (incremental-context mode).

Coding agents do not only send their main conversation. Qwen Code, for
instance, also sends through the same model (same API endpoint):

* structured "side queries" forced through a single schema tool
  (``respond_in_schema``): memory recall selection (``selected_memories``),
  the AUTO-mode permission classifier (``shouldBlock``), session titles…;
* prompt suggestions (``[SUGGESTION MODE: …]`` appended to the history);
* background forked agents with their own system prompt and tools: managed
  memory extraction, memory "dream" consolidation, skill review…;
* real side tasks that need a real answer: sub-agents, web-page summaries,
  /btw questions, recaps…

In incremental mode there is ONE web discussion. Typing every side query into
it mixed several "agents" in the same discussion: the model answered the main
task with the memory sub-agent's tool calls, side queries without any
assistant message were taken for a new coding session (whole prompts were
typed again), etc.

This module recognises the background / structured side queries so the proxy
can answer them LOCALLY, instantly, without touching the web discussion. The
other side tasks are typed into the discussion as clearly framed side requests
by ``services.sessions`` (their own state, the main conversation is untouched).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tools_format import _extract_text

# prompt ids used by Qwen Code for background requests (sent in `metadata`
# by some providers) — recognised when present
INTERNAL_PROMPT_IDS = {"prompt_suggestion", "forked_query", "speculation"}
SIDE_QUERY_PREFIX = "side-query:"

# (kind, marker) — matched case-insensitively at the start of the system prompt
# or of the last user message
HOUSEKEEPING_MARKERS = [
    ("suggestion", "[SUGGESTION MODE:"),
    ("memory", "You are now acting as the managed memory extraction subagent"),
    ("memory", "You are performing a managed memory dream"),
    ("memory", "You are consolidating durable, cross-project User Memory"),
    ("memory", "You are reviewing this conversation to extract reusable skills"),
]

# A boolean in the schema with such a name is a safety decision: never invent
# an answer for it — fail closed so the agent falls back to asking the user.
_SAFETY_KEY_RE = re.compile(r"block|deny|denied|reject|unsafe|danger|risk|malicious|harm|forbid", re.I)

SCHEMA_TOOL_NAMES = {"respond_in_schema"}

LOCAL_MEMORY_TEXT = "No durable information to save; no file changes."


@dataclass
class LocalAnswer:
    kind: str  # "suggestion" | "memory" | "schema" | "safety"
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None  # set → answered with an HTTP 400 (fail-closed)
    label: str = ""


def _fn(tool: Any) -> Dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    return tool.get("function") if isinstance(tool.get("function"), dict) else tool


def _system_text(messages: List[Dict[str, Any]]) -> str:
    parts = []
    for m in messages:
        if isinstance(m, dict) and str(m.get("role") or "").lower() in ("system", "developer"):
            parts.append(_extract_text(m.get("content")))
        else:
            break
    return "\n".join(parts)


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and str(m.get("role") or "").lower() == "user":
            return _extract_text(m.get("content"))
    return ""


def _starts_with(text: str, marker: str) -> bool:
    return text.lstrip()[: len(marker) + 40].lower().startswith(marker.lower())


def schema_default(schema: Any, depth: int = 0) -> Any:
    """Neutral value for a JSON schema ("do nothing" answer)."""
    if not isinstance(schema, dict) or depth > 12:
        return None
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return schema_default(schema[key][0], depth + 1)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "null")
    if typ == "object" or (typ is None and isinstance(schema.get("properties"), dict)):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else list(props)
        return {k: schema_default(props.get(k), depth + 1) for k in required}
    if typ == "array":
        return []
    if typ == "string":
        return ""
    if typ in ("number", "integer"):
        return 0
    if typ == "boolean":
        return False
    return None


def _safety_keys(schema: Any, depth: int = 0) -> List[str]:
    out: List[str] = []
    if not isinstance(schema, dict) or depth > 12:
        return out
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for k, v in props.items():
        if _SAFETY_KEY_RE.search(str(k)) and isinstance(v, dict) and v.get("type") == "boolean":
            out.append(str(k))
        out.extend(_safety_keys(v, depth + 1))
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            out.extend(_safety_keys(sub, depth + 1))
    if isinstance(schema.get("items"), dict):
        out.extend(_safety_keys(schema["items"], depth + 1))
    return out


def _forced_schema_tool(tools: Optional[List[Any]], tool_choice: Any) -> Optional[Dict[str, Any]]:
    fns = [_fn(t) for t in tools or [] if _fn(t).get("name")]
    if not fns:
        return None
    if isinstance(tool_choice, dict):
        name = (_fn(tool_choice) or {}).get("name") or tool_choice.get("name")
        if name:
            return next((f for f in fns if f.get("name") == name), None)
    if len(fns) == 1:
        f = fns[0]
        if f.get("name") in SCHEMA_TOOL_NAMES:
            return f
        if tool_choice in ("required", "any"):
            return f
    return None


def classify_local(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[LocalAnswer]:
    """LocalAnswer when the request is a background / structured side query."""
    extra = extra or {}
    system = _system_text(messages)
    last_user = _last_user_text(messages)

    for kind, marker in HOUSEKEEPING_MARKERS:
        if _starts_with(system, marker) or _starts_with(last_user, marker):
            if kind == "suggestion":
                return LocalAnswer("suggestion", text="", label="suggestion de saisie")
            return LocalAnswer("memory", text=LOCAL_MEMORY_TEXT, label="sous-agent mémoire")

    schema_tool = _forced_schema_tool(tools, extra.get("tool_choice"))
    if schema_tool is not None:
        params = schema_tool.get("parameters") if isinstance(schema_tool.get("parameters"), dict) else {}
        unsafe = _safety_keys(params)
        name = str(schema_tool.get("name"))
        if unsafe:
            return LocalAnswer(
                "safety",
                error=(
                    f"NemApi (incremental context): safety side query '{name}' ({', '.join(unsafe)}) "
                    f"is not answered automatically; falling back to manual approval."
                ),
                label=f"classifieur de sécurité ({', '.join(unsafe)})",
            )
        args = schema_default(params)
        if not isinstance(args, dict):
            args = {}
        call = {
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }
        keys = ", ".join(args) or "∅"
        return LocalAnswer("schema", tool_calls=[call], label=f"requête structurée {name}({keys})")

    rf = extra.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        js = rf.get("json_schema") if isinstance(rf.get("json_schema"), dict) else {}
        schema = js.get("schema") if isinstance(js.get("schema"), dict) else {}
        if _safety_keys(schema):
            return LocalAnswer("safety", error="NemApi: safety side query not answered automatically.",
                               label="classifieur de sécurité")
        return LocalAnswer("schema", text=json.dumps(schema_default(schema) or {}, ensure_ascii=False),
                           label=f"réponse JSON {js.get('name') or ''}".strip())

    meta = extra.get("metadata")
    pid = meta.get("promptId") if isinstance(meta, dict) else None
    if isinstance(pid, str) and (pid in ("prompt_suggestion", "speculation")):
        return LocalAnswer("suggestion", text="", label=f"requête interne {pid}")
    return None
