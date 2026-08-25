"""Message formatting, response cleaning, and tool-call parsing for NemApi.

Robust handling of DOM-extracted text (UI chrome, control chars, HTML entities,
broken fences, thinking blocks) so coding agents (Qwen Code, Cursor, Continue,
Aider, etc.) can parse responses and code blocks reliably.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# UI / chrome noise (buttons, labels, i18n)
# ---------------------------------------------------------------------------
UI_LINE_RE = re.compile(
    r"^(Copy|Copied|Download|JSON|Think|Thinking|Regenerate|Retry|Share|Stop|Edit|"
    r"Like|Dislike|Report|复制|下载|重新生成|复制代码|全屏|展开|收起|"
    r"Copy code|Show more|Show less|Collapse|Expand)\s*[:：.]?\s*$",
    re.I,
)
# Inline chrome only — never match short words that appear in code/filenames
# (e.g. "json" in package.json, "stop", "edit", "share").
UI_INLINE_RE = re.compile(
    r"\b(Copy code|Copied|Download|Regenerate|Show more|Show less|"
    r"重新生成|复制代码)\b",
    re.I,
)

# Control / invisible characters that break JSON parsers and terminal agents
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u206f"
    r"\ufeff\ufff9-\ufffb]"
)

# Common HTML entities that sometimes leak from DOM textContent quirks
_ENTITY_RE = re.compile(
    r"&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-fA-F]+);"
)

# Thinking / reasoning blocks (various providers)
_THINK_BLOCK_RE = re.compile(
    r"(?:"
    r"<think>[\s\S]*?</think>|"
    r"<thinking>[\s\S]*?</thinking>|"
    r"```(?:think|thinking|reason|reasoning)[\s\S]*?```|"
    r"<details[^>]*>[\s\S]*?</details>"
    r")",
    re.I,
)

# Broken or empty code fences left by DOM extraction
_EMPTY_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*```")
_ORPHAN_FENCE_RE = re.compile(r"(?:^|\n)(```+)(?!\s*[a-zA-Z0-9_+-]|\n)")


def _strip_control_chars(s: str) -> str:
    """Remove C0/C1 controls and Unicode format/invisible chars (keep \n \t \r)."""
    if not s:
        return ""
    # Normalize to NFC first (helps with composed characters in code)
    s = unicodedata.normalize("NFC", s)
    return _CONTROL_RE.sub("", s)


def _decode_entities(s: str) -> str:
    """Decode HTML entities if present; leave already-clean text alone."""
    if not s or "&" not in s:
        return s
    # Only decode when we see real entities to avoid corrupting intentional & in code
    if not _ENTITY_RE.search(s):
        return s
    try:
        return html.unescape(s)
    except Exception:
        return s


def clean_assistant_text(text: str) -> str:
    """Sanitize DOM-extracted assistant text for agents and JSON transport.

    - Strips thinking / details blocks
    - Removes UI chrome lines and inline labels (outside code fences)
    - Decodes HTML entities when present
    - Removes invisible / control characters that break parsers
    - Normalizes newlines and collapses excessive blank lines
    - Tidies empty or orphaned code fences
    """
    t = str(text or "")
    if not t.strip():
        return ""

    t = _strip_control_chars(t)
    t = _decode_entities(t)
    t = _THINK_BLOCK_RE.sub("", t)

    # Line-level UI chrome
    lines: List[str] = []
    for line in t.split("\n"):
        if UI_LINE_RE.match(line.strip()):
            continue
        lines.append(line)
    t = "\n".join(lines)

    # Inline UI words, but never inside a code fence
    def _sub_ui(m: re.Match) -> str:
        before = t[: m.start()]
        if (before.count("```") % 2) == 1:
            return m.group(0)
        return ""

    t = UI_INLINE_RE.sub(_sub_ui, t)

    # Whitespace hygiene (preserve indentation inside fences as much as possible)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _EMPTY_FENCE_RE.sub("", t)

    # Fix occasional orphan opening fences that leave agents confused
    # (only remove a lone ``` that is not followed by a language or content)
    # Keep conservative: only collapse triple+ empty runs
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content)
    return str(content)


def serialize_tools_prompt(tools: Optional[List[Dict[str, Any]]]) -> str:
    if not tools:
        return ""
    lines = []
    for t in tools:
        fn = (t or {}).get("function") or t or {}
        name = fn.get("name")
        if not name:
            continue
        desc = fn.get("description") or ""
        try:
            params = json.dumps(fn.get("parameters") or {}, ensure_ascii=False)
        except Exception:
            params = "{}"
        lines.append(f"- {name}: {desc}\n  parameters: {params}".rstrip())
    if not lines:
        return ""
    return "\n".join(
        [
            "You can call tools. To call a tool, output ONLY this exact block (no markdown fence):",
            '<tool>{"name": "<tool_name>", "arguments": { ... }}</tool>',
            "Rules:",
            "- Use exactly <tool>...</tool>. Do NOT invent other wrappers.",
            '- "name" must be one of the tools below; "arguments" must be a JSON object.',
            "- When a tool is needed, emit the <tool> block instead of only describing the plan.",
            "- If no tool is needed, answer normally without any <tool> block.",
            "",
            "Available tools:",
            *lines,
        ]
    )


def format_messages_prompt(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build a single prompt string with roles + optional tools contract.

    Designed so the web UI of any provider receives a clear, role-labelled
    transcript that coding agents (Qwen Code, Cursor, etc.) already produce.
    """
    system_parts: List[str] = []
    tool_sys = serialize_tools_prompt(tools)
    if tool_sys:
        system_parts.append(tool_sys)

    lines: List[str] = []
    call_name_by_id: Dict[str, str] = {}
    saw_tools = False

    for m in messages:
        role = (m.get("role") or "").lower()
        if role == "system":
            t = _extract_text(m.get("content")).strip()
            if t:
                system_parts.append(t)
        elif role == "user":
            t = _extract_text(m.get("content")).strip()
            if t:
                lines.append(f"User: {t}")
        elif role == "assistant":
            t = _extract_text(m.get("content")).strip()
            parts = []
            if t:
                parts.append(t)
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args or {}, ensure_ascii=False)
                if c.get("id"):
                    call_name_by_id[c["id"]] = name
                parts.append(f'<tool>{{"name": {json.dumps(name)}, "arguments": {raw_args}}}</tool>')
                saw_tools = True
            if parts:
                lines.append("Assistant: " + "\n".join(parts))
        elif role == "tool":
            t = _extract_text(m.get("content")).strip()
            name = call_name_by_id.get(m.get("tool_call_id") or "") or m.get("name") or "tool"
            lines.append(f"Tool result ({name}): {t or '(no output)'}")
            saw_tools = True
        else:
            t = _extract_text(m.get("content")).strip()
            if t:
                lines.append(t)

    chunks: List[str] = []
    if system_parts:
        chunks.append("\n\n".join(system_parts))
    if lines:
        chunks.append("\n\n".join(lines))
    if saw_tools:
        chunks.append(
            "Continue using the tool results above. Do NOT repeat successful tool calls; "
            "take the next step or give the final answer."
        )
    return "\n\n".join(chunks).strip()


_TOOL_BLOCK_RE = re.compile(r"<(tool_call|tool)(?:\s[^>]*)?>([\s\S]*?)</\1>", re.I)
_TOOL_SUFFIX_RE = re.compile(r"<tool:([A-Za-z0-9_.+-]+)>([\s\S]*?)</tool>", re.I)
# Qwen / some agents also emit function-call style XML
_FUNCTION_CALL_RE = re.compile(
    r"<function[_-]?call(?:\s[^>]*)?>([\s\S]*?)</function[_-]?call>",
    re.I,
)
_INVOKE_RE = re.compile(
    r"<invoke\s+name=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</invoke>",
    re.I,
)


def _normalize_args(raw: Any) -> str:
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return "{}"
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", s)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    if isinstance(obj, dict):
                        return json.dumps(obj, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            return json.dumps({"raw": s}, ensure_ascii=False)
    return "{}"


def parse_tool_calls(
    text: str,
    requested_names: Optional[List[str]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    if not text:
        return "", []

    ranges: List[Tuple[int, int]] = []
    extracted: List[Tuple[str, str]] = []

    def handle_inner(inner: str, fallback: str = "") -> Optional[Tuple[str, str]]:
        inner = inner.strip()
        if not inner:
            return None
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", inner.strip())
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("tool") or obj.get("type") or ""
                if "arguments" in obj:
                    args = obj["arguments"]
                elif "params" in obj or "parameters" in obj:
                    args = obj.get("params") or obj.get("parameters")
                elif name:
                    args = {k: v for k, v in obj.items() if k not in ("name", "tool", "type")}
                else:
                    name = fallback
                    args = obj
                if not name:
                    name = fallback
                if name:
                    return str(name), _normalize_args(args)
        except json.JSONDecodeError:
            if fallback:
                return fallback, _normalize_args(inner)
        return None

    for m in _TOOL_SUFFIX_RE.finditer(text):
        parsed = handle_inner(m.group(2), m.group(1))
        if parsed:
            ranges.append((m.start(), m.end()))
            extracted.append(parsed)

    for m in _TOOL_BLOCK_RE.finditer(text):
        if any(s <= m.start() < e for s, e in ranges):
            continue
        parsed = handle_inner(m.group(2))
        if parsed:
            ranges.append((m.start(), m.end()))
            extracted.append(parsed)

    for m in _FUNCTION_CALL_RE.finditer(text):
        if any(s <= m.start() < e for s, e in ranges):
            continue
        parsed = handle_inner(m.group(1))
        if parsed:
            ranges.append((m.start(), m.end()))
            extracted.append(parsed)

    for m in _INVOKE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in ranges):
            continue
        name = m.group(1)
        inner = m.group(2)
        # Try to collect <parameter name="...">value</parameter>
        params: Dict[str, Any] = {}
        for pm in re.finditer(
            r"<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</parameter>",
            inner,
            re.I,
        ):
            params[pm.group(1)] = pm.group(2).strip()
        if params:
            ranges.append((m.start(), m.end()))
            extracted.append((name, _normalize_args(params)))
        else:
            parsed = handle_inner(inner, name)
            if parsed:
                ranges.append((m.start(), m.end()))
                extracted.append(parsed)

    if not extracted:
        return text, []

    tool_calls = []
    for name, args in extracted:
        if requested_names:
            lower = {n.lower(): n for n in requested_names}
            if name.lower() in lower:
                name = lower[name.lower()]
            else:
                for rn in requested_names:
                    if name.lower() in rn.lower() or rn.lower() in name.lower():
                        name = rn
                        break
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )

    ranges.sort()
    out = []
    pos = 0
    for s, e in ranges:
        if s > pos:
            out.append(text[pos:s])
        pos = max(pos, e)
    out.append(text[pos:])
    remaining = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    return remaining, tool_calls
