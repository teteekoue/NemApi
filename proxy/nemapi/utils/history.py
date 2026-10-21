"""Conversation fingerprinting + duplicate filtering for the incremental-context mode.

Coding agents (Qwen Code, Cline, Continue, Aider…) resend the WHOLE history on
every request, while the provider's web discussion already contains it. In
incremental mode NemApi keeps ONE web discussion and only types what that
discussion does not contain yet:

* every message gets a short normalised fingerprint (tolerant to what clients
  change when they echo our answers back: whitespace, tool-call ids, JSON
  spacing / key order of tool arguments, ``content: ""`` vs ``None``, list vs
  string content);
* ``select_new()`` compares the request with what the discussion already holds
  and returns only the messages that were never typed;
* the system prompt is filtered line by line and the tools contract tool by
  tool, so a changed system prompt / a new tool only sends what changed.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .tools_format import TOOL_CALL_SHAPE, TOOL_FENCE, _extract_text, serialize_tools_prompt

TOOL_REMINDER = (
    f"Reminder: to call a tool, write {TOOL_CALL_SHAPE} inside a {TOOL_FENCE}tool code block "
    f"(closed by {TOOL_FENCE}); otherwise answer normally."
)
TOOL_RESULTS_FOOTER = (
    "Continue using the tool results above. Do NOT repeat successful tool calls; "
    "take the next step or give the final answer."
)
NUDGE = "Please answer my last message above."

# Side requests of the agent (sub-agents, summaries, /btw…) share the discussion
# with the main conversation: they are framed so the model never mixes them up.
SIDE_HEADER = (
    "[SIDE REQUEST from the agent — this is NOT the next step of the main task. "
    "Handle it on its own: follow only the instructions in this message, answer only this request. "
    "The main conversation will resume afterwards.]"
)
SIDE_SYSTEM_TITLE = "Instructions for this side request:"
BACK_TO_MAIN = (
    "[BACK TO THE MAIN CONVERSATION — ignore the side request(s) above and their instructions; "
    "continue the main task where it was.]"
)

MAIN_START = "[MAIN CONVERSATION — the side request(s) above were separate tasks; this is the main task.]"


def side_request_prompt(body: str, tool_names: Sequence[str], label: str = "") -> str:
    task_line = f"Side task: {label}" if label else ""
    if tool_names:
        tools_line = (
            "Tools available for this side request ONLY: " + ", ".join(tool_names)
            + f". Call them with {TOOL_CALL_SHAPE} inside a {TOOL_FENCE}tool code block."
        )
    else:
        tools_line = "No tool is available for this side request: answer with plain text only."
    head = SIDE_HEADER + ("\n" + task_line if task_line else "")
    return "\n\n".join(x for x in (head, tools_line, body) if x).strip()


def _ws(text: str) -> str:
    return " ".join((text or "").split())


def _norm_args(raw: Any) -> str:
    if isinstance(raw, (dict, list)):
        obj = raw
    else:
        try:
            obj = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except (ValueError, TypeError):
            return _ws(str(raw))
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return _ws(str(obj))


def _content_text(content: Any) -> str:
    text = _extract_text(content)
    if isinstance(content, list):
        # images / files have no text: count them so two different screenshots
        # with the same caption are not considered identical
        extra = sum(1 for p in content if isinstance(p, dict) and p.get("type") not in (None, "text"))
        if extra:
            text += f" [+{extra} attachment(s)]"
    return text


def _hash(key: str) -> str:
    return hashlib.blake2b(key.encode("utf-8", "replace"), digest_size=12).hexdigest()


def _role(m: Dict[str, Any]) -> str:
    return str(m.get("role") or "").lower()


def message_fingerprint(m: Dict[str, Any]) -> str:
    role = _role(m)
    if role == "assistant":
        calls = m.get("tool_calls") or []
        if not calls and isinstance(m.get("function_call"), dict):
            calls = [{"function": m["function_call"]}]
        if calls:
            parts = []
            for c in calls:
                fn = (c or {}).get("function") or {}
                parts.append(f"{fn.get('name') or ''}:{_norm_args(fn.get('arguments'))}")
            # text next to tool calls is often dropped/rewritten by clients: ignore it
            return _hash("A|T|" + "|".join(parts))
        return _hash("A|" + _ws(_content_text(m.get("content"))))
    if role in ("tool", "function"):
        return _hash("T|" + _ws(_content_text(m.get("content"))))
    return _hash(f"{role or 'user'}|" + _ws(_content_text(m.get("content"))))


def system_line_fingerprints(text: str) -> Dict[str, str]:
    """fingerprint -> original line, for every non-empty line of the system prompt."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        norm = _ws(line)
        if norm:
            out.setdefault(_hash("L|" + norm), line.rstrip())
    return out


def tool_fingerprints(tools: Optional[List[Any]]) -> Dict[str, Any]:
    """fingerprint -> tool definition (name + description + JSON schema)."""
    out: Dict[str, Any] = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        key = json.dumps(
            {"n": fn.get("name"), "d": fn.get("description") or "", "p": fn.get("parameters") or {}},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        out[_hash("F|" + key)] = t
    return out


@dataclass
class History:
    """A request's messages split into leading system prompt + fingerprint chain."""

    messages: List[Dict[str, Any]]
    system_text: str
    chain: List[str]  # fingerprints of messages after the leading system block
    roles: List[str]  # role of each chain entry
    index: List[int]  # chain position -> index in `messages`

    @classmethod
    def from_messages(cls, messages: Sequence[Dict[str, Any]]) -> "History":
        msgs = list(messages)
        i = 0
        system_parts = []
        while i < len(msgs) and _role(msgs[i]) == "system":
            system_parts.append(_content_text(msgs[i].get("content")))
            i += 1
        chain, roles, index = [], [], []
        for j in range(i, len(msgs)):
            chain.append(message_fingerprint(msgs[j]))
            roles.append(_role(msgs[j]))
            index.append(j)
        return cls(msgs, "\n".join(system_parts), chain, roles, index)

    def message(self, pos: int) -> Dict[str, Any]:
        return self.messages[self.index[pos]]


def select_new(h: History, view: Sequence[str], sent: Sequence[str], answers: Iterable[str]) -> List[int]:
    """Chain positions of the messages the web discussion does not contain yet.

    * view    = the history as the agent saw it after our last answer
    * sent    = every message fingerprint typed into / answered in the discussion
    * answers = fingerprints of assistant messages present in the discussion

    Assistant messages are never re-typed: they are either our own answers
    (already in the discussion) or client-side rewrites of them.
    """
    chain, roles = h.chain, h.roles
    view = list(view)
    k = len(view)
    if k and len(chain) >= k and chain[:k] == view:
        # normal agent turn: history = what we know + new messages (positional,
        # so a repeated "continue" or an identical tool output IS sent again)
        return [p for p in range(k, len(chain)) if roles[p] != "assistant"]
    if chain and view[: len(chain)] == chain:
        # retry of a request whose content is already in the discussion
        return []

    # The history was rewritten (compaction, edit, dropped answer…).
    # Anchor on the LAST of our answers still present in the request: what comes
    # after it is a candidate, minus what the discussion holds after that answer.
    sent = list(sent)
    answers = set(answers)
    anchor = next((p for p in range(len(chain) - 1, -1, -1) if roles[p] == "assistant" and chain[p] in answers), None)
    if anchor is not None:
        pos = _best_occurrence(chain, anchor, sent)
        remaining = Counter(sent[pos + 1:])
        candidates = range(anchor + 1, len(chain))
    else:
        remaining = Counter(sent)
        candidates = range(len(chain))
    out = []
    for p in candidates:
        if roles[p] == "assistant":
            continue
        if remaining[chain[p]] > 0:
            remaining[chain[p]] -= 1  # already in the discussion: filtered out
            continue
        out.append(p)
    if not out and anchor is None and chain and roles[-1] != "assistant":
        # none of our answers is left in the history (full compaction) and the
        # filter removed everything: the trailing input is what the agent asks
        # about now, so it must be answered (e.g. a repeated "continue")
        p = len(chain) - 1
        while p >= 0 and roles[p] != "assistant":
            p -= 1
        out = list(range(p + 1, len(chain)))
    return out


def _best_occurrence(chain: Sequence[str], anchor: int, sent: Sequence[str]) -> int:
    """Position in `sent` of the answer chain[anchor].

    The same answer may appear several times ("Done.", identical tool call):
    pick the occurrence whose preceding messages match the request best, the
    latest one on ties (compaction drops the beginning of the history).
    """
    best, best_score = -1, -1
    for i, fp in enumerate(sent):
        if fp != chain[anchor]:
            continue
        score, a, b = 0, anchor - 1, i - 1
        while a >= 0 and b >= 0 and chain[a] == sent[b]:
            score, a, b = score + 1, a - 1, b - 1
        if score >= best_score:
            best, best_score = i, score
    return best


def format_delta_prompt(
    h: History,
    positions: Sequence[int],
    tools: Optional[List[Any]] = None,
    new_tools: Optional[List[Any]] = None,
    new_system_lines: Optional[List[str]] = None,
    system_title: str = "System instructions (update):",
    reminder: bool = True,
) -> str:
    """Prompt with only the given chain positions (+ system/tool updates).

    Same conventions as format_messages_prompt() (which produced the first
    prompt of the discussion) so the web model sees a consistent transcript.
    """
    call_name_by_id: Dict[str, str] = {}
    for m in h.messages:
        if _role(m) == "assistant":
            for c in m.get("tool_calls") or []:
                if isinstance(c, dict) and c.get("id"):
                    call_name_by_id[c["id"]] = ((c.get("function") or {}).get("name")) or ""

    delta = [h.message(p) for p in positions]
    lines: List[str] = []
    saw_tool_result = False
    single_user = len(delta) == 1 and _role(delta[0]) == "user" and not new_system_lines and not new_tools

    for m in delta:
        role = _role(m)
        text = _content_text(m.get("content")).strip()
        if role in ("tool", "function"):
            name = call_name_by_id.get(m.get("tool_call_id") or "") or m.get("name") or "tool"
            lines.append(f"Tool result ({name}): {text or '(no output)'}")
            saw_tool_result = True
        elif role == "system":
            if text:
                lines.append(f"System: {text}")
        elif text:
            lines.append(text if single_user else f"User: {text}")

    chunks: List[str] = []
    if new_system_lines:
        chunks.append(system_title + "\n" + "\n".join(new_system_lines))
    if new_tools:
        chunks.append(serialize_tools_prompt(new_tools))
    chunks.append("\n\n".join(lines))
    if saw_tool_result:
        chunks.append(TOOL_RESULTS_FOOTER)
    if reminder and tools and not new_tools:
        chunks.append(TOOL_REMINDER)
    return "\n\n".join(c for c in chunks if c).strip()
