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
_EMPTY_FENCE_RE = re.compile(r"(?m)^[ \t]*(`{3,}|~{3,})[a-zA-Z0-9_+-]*[ \t]*\n\s*\1[`~]*[ \t]*$")
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
    except (ValueError, TypeError):
        return s


# Spans that must reach the agent byte-for-byte: fenced code blocks and tool
# call blocks (their JSON carries file contents / shell commands — decoding
# "&amp;", dropping a "<details>" element or a "Copy" line there would corrupt
# the file written by the agent).
_FENCE_SPAN_RE = re.compile(r"(?m)^[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:\n[ \t]*\1[`~]*[ \t]*(?=\n|$)|\Z)")
_TOOL_SPAN_RE = re.compile(
    r"<(tool_call|tool)(?:[\s:][^>]*)?>[\s\S]*?(?:</\1(?::[^>]*)?\s*>|(?=<(?:tool_call|tool)(?:[\s:>]))|\Z)",
    re.I,
)
# "[m.to](https://m.to/)": a markdown renderer auto-linked a bare word that
# looks like a domain ("m.to", "a.sh", "x.py"…) and the DOM extraction wrote
# it back as a link. Undo it when the label is the URL itself.
_AUTOLINK_RE = re.compile(r"\[([^\[\]\s]{1,200})\]\(([^()\s]{1,300})\)")


def _undo_autolinks(s: str) -> str:
    if "](" not in s:
        return s

    def sub(m: re.Match) -> str:
        label, href = m.group(1), m.group(2)
        bare = re.sub(r"^(?:https?://|mailto:)", "", href, flags=re.I).rstrip("/")
        lab = re.sub(r"^(?:https?://|mailto:)", "", label, flags=re.I).rstrip("/")
        if lab and bare.lower() == lab.lower():
            return label
        return m.group(0)

    return _AUTOLINK_RE.sub(sub, s)


def _split_protected(t: str) -> List[Tuple[bool, str]]:
    """Split into (protected?, segment). Fences win over tool blocks."""
    spans: List[Tuple[int, int, bool]] = []  # (start, end, is_fence)
    for m in _FENCE_SPAN_RE.finditer(t):
        spans.append((m.start(), m.end(), True))
    for rx in (_TOOL_SPAN_RE, _DSML_BLOCK_RE, _DSML_INVOKE_RE, _QWEN_FUNC_RE, _FUNCTION_CALL_RE, _INVOKE_RE):
        for m in rx.finditer(t):
            if not any(s <= m.start() < e for s, e, _ in spans):
                spans.append((m.start(), m.end(), False))
    spans.sort()
    out: List[Tuple[bool, str]] = []
    pos = 0
    for s0, e0, fence in spans:
        if s0 < pos:
            continue
        if s0 > pos:
            out.append((False, t[pos:s0]))
        seg = t[s0:e0]
        # tool blocks written outside a fence went through the markdown
        # renderer: undo its auto-links (fenced code is shown verbatim)
        out.append((True, seg if fence else _undo_autolinks(seg)))
        pos = e0
    if pos < len(t):
        out.append((False, t[pos:]))
    return out


def _clean_prose(t: str) -> str:
    t = _decode_entities(t)
    t = re.sub(r"<details[^>]*>[\s\S]*?</details>", "", t, flags=re.I)
    lines: List[str] = []
    for line in t.split("\n"):
        if UI_LINE_RE.match(line.strip()):
            continue
        lines.append(line)
    t = "\n".join(lines)
    t = UI_INLINE_RE.sub("", t)
    t = _undo_autolinks(t)
    return re.sub(r"[ \t]+\n", "\n", t)


def clean_assistant_text(text: str) -> str:
    """Sanitize DOM-extracted assistant text for agents and JSON transport.

    Source of truth for final agent-facing cleanup (JS providers do a lighter
    DOM-side pass; this function runs on the proxy after the browser returns).

    - Strips thinking blocks
    - Removes UI chrome lines and inline labels, decodes HTML entities,
      undoes renderer auto-links — but never inside code fences or tool-call
      blocks, which are passed through verbatim
    - Removes invisible / control characters that break parsers
    - Normalizes newlines and collapses excessive blank lines
    - Tidies empty code fences
    """
    t = str(text or "")
    if not t.strip():
        return ""

    t = _strip_control_chars(t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"<think>[\s\S]*?</think>|<thinking>[\s\S]*?</thinking>", "", t, flags=re.I)
    t = re.sub(r"(?m)^[ \t]*```(?:think|thinking|reason|reasoning)\b[\s\S]*?```", "", t, flags=re.I)

    t = "".join(seg if protected else _clean_prose(seg) for protected, seg in _split_protected(t))

    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _EMPTY_FENCE_RE.sub("", t)
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


TOOL_FENCE = "````"
TOOL_CALL_SHAPE = '<tool>{"name": "<tool_name>", "arguments": { ... }}</tool>'


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
        except (TypeError, ValueError):
            params = "{}"
        lines.append(f"- {name}: {desc}\n  parameters: {params}".rstrip())
    if not lines:
        return ""
    return "\n".join(
        [
            "You can call tools. To call a tool, write a code block fenced with FOUR backticks "
            "and the word tool, containing exactly one <tool> line:",
            TOOL_FENCE + "tool",
            TOOL_CALL_SHAPE,
            TOOL_FENCE,
            "Rules:",
            "- ALWAYS put the <tool> line inside that " + TOOL_FENCE + "tool fence: this page renders markdown, "
            "and only text inside a code block reaches the tool unchanged (outside it, backslashes, $ signs, "
            "quotes and URLs get altered).",
            "- Do NOT use any other tool-call syntax (no DSML, no <function_calls>, no <invoke>/<parameter> tags).",
            "- One fenced <tool> block per call; for several calls write several fenced blocks.",
            "- The content must be valid JSON on one line: inside strings write \\\" for a double quote, "
            "\\\\ for a backslash and \\n for a newline (so a literal \\n in a shell or sed command is "
            "written \\\\n).",
            "- For multi-line file content prefer a file writing/editing tool (if available) over shell heredocs or echo.",
            '- "name" must be one of the tools below; "arguments" must be a JSON object.',
            "- When a tool is needed, emit the fenced <tool> block instead of only describing the plan.",
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


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------
# Web models do not always follow the <tool>{json}</tool> contract. Besides it,
# the parser understands (and never shows to the agent as text):
#   * <tool_call>{json}</tool_call>, <tool:name>{json}</tool>, <function_call>…
#   * DeepSeek native DSML, in every form seen in the wild:
#       <｜DSML｜tool_calls> / <｜DSML｜function_calls> / <｜｜DSML｜｜ calls> /
#       <||DSML||tool_calls>  … <｜DSML｜invoke name="x"> <｜DSML｜parameter
#       name="k" string="true|false">v</｜DSML｜parameter> …
#     (full-width or ASCII bars, single or doubled, optional spaces, missing
#     closing tags, several invokes per block, invokes without a block)
#   * DeepSeek V3 special tokens (<｜tool▁call▁begin｜>function<｜tool▁sep｜>…)
#   * Qwen3-Coder XML (<function=name><parameter=k>v</parameter></function>)
#   * Anthropic-style <invoke name="x"><parameter name="k">v</parameter></invoke>
#   * an unclosed <tool>{json} at the end of the answer
#   * a fenced ```json {"name": <known tool>, "arguments": {…}}``` block
# JSON is parsed leniently (raw newlines, unescaped inner quotes, trailing
# commas, missing closing braces, Python-style dicts) because the text comes
# from a rendered web page. Stray DSML tags / special tokens are removed from
# the content in every case.

_BAR = r"[｜|│┃]"
_DSML = rf"{_BAR}{{1,2}}\s*DSML\s*{_BAR}{{1,2}}\s*"
_DSML_BLOCK = r"(?:tool_calls|function_calls|tool_call|calls)"
_DSML_BLOCK_RE = re.compile(
    rf"<\s*{_DSML}{_DSML_BLOCK}\s*>([\s\S]*?)(?:<\s*/\s*{_DSML}{_DSML_BLOCK}\s*>|\Z)", re.I
)
_DSML_INVOKE_RE = re.compile(
    rf"<\s*{_DSML}invoke\s+name\s*=\s*[\"'“”]?([^\"'“”>\s]+)[\"'“”]?[^>]*>([\s\S]*?)"
    rf"(?:<\s*/\s*{_DSML}invoke\s*>|(?=<\s*{_DSML}invoke\b)|(?=<\s*/\s*{_DSML}{_DSML_BLOCK}\s*>)|\Z)",
    re.I,
)
_DSML_PARAM_RE = re.compile(
    rf"<\s*{_DSML}parameter\s+name\s*=\s*[\"'“”]?([^\"'“”>\s]+)[\"'“”]?([^>]*)>([\s\S]*?)"
    rf"(?:<\s*/\s*{_DSML}parameter\s*>|(?=<\s*/?\s*{_DSML}(?:parameter|invoke)\b)|\Z)",
    re.I,
)
_DSML_TAG_RE = re.compile(rf"<\s*/?\s*{_DSML}[^<>]*>", re.I)
_SPECIAL_TOKEN_RE = re.compile(
    rf"<\s*{_BAR}\s*(?:end|begin)[▁_ ]of[▁_ ]sentence\s*{_BAR}\s*>|"
    rf"<\s*{_BAR}\s*tool[▁_ ](?:calls?|outputs?)[▁_ ](?:begin|end)\s*{_BAR}\s*>|"
    rf"<\s*{_BAR}\s*tool[▁_ ]sep\s*{_BAR}\s*>",
    re.I,
)
_DSV3_CALL_RE = re.compile(
    rf"<\s*{_BAR}\s*tool[▁_ ]call[▁_ ]begin\s*{_BAR}\s*>\s*(?:function)?\s*"
    rf"<\s*{_BAR}\s*tool[▁_ ]sep\s*{_BAR}\s*>\s*([A-Za-z0-9_.:-]+)\s*([\s\S]*?)"
    rf"<\s*{_BAR}\s*tool[▁_ ]call[▁_ ]end\s*{_BAR}\s*>",
    re.I,
)
_TOOL_BLOCK_RE = re.compile(r"<(tool_call|tool)(?:\s[^>]*)?>([\s\S]*?)</\1\s*>", re.I)
_TOOL_OPEN_RE = re.compile(r"<(tool_call|tool)(?:\s[^>]*)?>", re.I)
_TOOL_SUFFIX_RE = re.compile(r"<tool:([A-Za-z0-9_.+-]+)>([\s\S]*?)</tool(?::[A-Za-z0-9_.+-]+)?\s*>", re.I)
_FUNCTION_CALL_RE = re.compile(
    r"<function[_-]?call(?:\s[^>]*)?>([\s\S]*?)</function[_-]?call>",
    re.I,
)
_QWEN_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>([\s\S]*?)(?:</function\s*>|\Z)", re.I)
_QWEN_PARAM_RE = re.compile(
    r"<parameter=([^>\s]+)\s*>([\s\S]*?)(?:</parameter\s*>|(?=<parameter=)|(?=</function)|\Z)", re.I
)
_INVOKE_RE = re.compile(
    r"<invoke\s+name=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</invoke>",
    re.I,
)
_XML_PARAM_RE = re.compile(r"<parameter\s+name=[\"']([^\"']+)[\"']([^>]*)>([\s\S]*?)</parameter>", re.I)
_FENCED_JSON_RE = re.compile(
    r"(`{3,}|~{3,})[ \t]*(?:json|tool|tool_call|javascript|js)?[ \t]*\n(\s*[\[{][\s\S]*?)\n?[ \t]*\1[`~]*", re.I
)
_EMPTY_FENCE_AFTER_RE = re.compile(r"(`{3,}|~{3,})[A-Za-z0-9_+-]*[ \t]*\n?\s*\1[`~]*")


def _strip_fence(s: str) -> str:
    s = s.strip()
    m = re.match(r"^(`{3,}|~{3,})[A-Za-z0-9_+-]*[ \t]*\n?([\s\S]*?)\n?\1[`~]*$", s)
    return m.group(2).strip() if m else s


def _repair_json(s: str) -> str:
    """Best-effort repair of JSON written by a chat model / mangled by a web page.

    Escapes raw control characters and unescaped quotes inside strings,
    doubles invalid backslash escapes, drops trailing commas, closes what is
    left open. Stops at the end of the first top-level value.
    """
    out: List[str] = []
    stack: List[str] = []
    in_str = False
    started = False
    i, n = 0, len(s)
    hexd = "0123456789abcdefABCDEF"

    def last_sig() -> int:
        k = len(out) - 1
        while k >= 0 and out[k] in (" ", "\t", "\n", "\r"):
            k -= 1
        return k

    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                nxt = s[i + 1] if i + 1 < n else ""
                if nxt and nxt in "\"\\/bfnrt":
                    out.append(c + nxt)
                    i += 2
                    continue
                if nxt == "u" and len(s) >= i + 6 and all(ch in hexd for ch in s[i + 2:i + 6]):
                    out.append(s[i:i + 6])
                    i += 6
                    continue
                out.append("\\\\")
                i += 1
                continue
            if c == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                nxt = s[j] if j < n else ""
                closes = nxt in (",", ":", "}", "]", "")
                if closes and nxt == ",":
                    k = j + 1
                    while k < n and s[k] in " \t\r\n":
                        k += 1
                    closes = k >= n or s[k] in "\"{[-0123456789tfn}]"
                if closes and nxt == ":" and stack and stack[-1] == "]":
                    closes = False  # keys only exist in objects
                if closes:
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
                i += 1
                continue
            if c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            elif ord(c) < 0x20:
                out.append("\\u%04x" % ord(c))
            else:
                out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
            started = True
            out.append(c)
        elif c in "}]":
            k = last_sig()
            if k >= 0 and out[k] == ",":
                del out[k]
            if stack and stack[-1] == c:
                stack.pop()
            out.append(c)
            if started and not stack:
                break
        else:
            out.append(c)
        i += 1
    _repair_json.auto_closed = bool(in_str or stack or not started)  # type: ignore[attr-defined]
    if in_str:
        out.append('"')
    k = last_sig()
    if k >= 0 and out[k] == ",":
        del out[k]
    out.extend(reversed(stack))
    return "".join(out)


def loads_lenient(raw: str) -> Any:
    """json.loads that survives what web UIs and chat models do to JSON."""
    s = _strip_fence(str(raw or ""))
    if not s:
        raise ValueError("empty")
    for strict in (True, False):
        try:
            return json.loads(s, strict=strict)
        except (ValueError, TypeError):
            pass
    idx = min([p for p in (s.find("{"), s.find("[")) if p >= 0], default=-1)
    if idx < 0:
        raise ValueError("no JSON value")
    body = s[idx:]
    dec = json.JSONDecoder(strict=False)
    try:
        return dec.raw_decode(body)[0]
    except ValueError:
        pass
    try:
        return dec.raw_decode(_repair_json(body))[0]
    except ValueError:
        pass
    try:
        import ast

        val = ast.literal_eval(body)
        if isinstance(val, (dict, list)):
            return val
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        pass
    raise ValueError("invalid JSON")


def _args_to_dict(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            obj = loads_lenient(s)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        return {"raw": s}
    return {"value": raw}


def _normalize_args(raw: Any) -> str:
    return json.dumps(_args_to_dict(raw), ensure_ascii=False)


def _param_value(raw: str, attrs: str = "") -> Any:
    """Value of a DSML/XML parameter: string="true" → raw text, "false" → JSON."""
    v = raw
    if "\n" in v:
        v = re.sub(r"^[ \t]*\r?\n", "", v, count=1)
        v = re.sub(r"\r?\n[ \t]*$", "", v, count=1)
    else:
        v = v.strip()
    m = re.search(r"string\s*=\s*[\"']?(true|false)", attrs or "", re.I)
    if m and m.group(1).lower() == "false":
        try:
            return loads_lenient(v) if v.strip()[:1] in "[{" else json.loads(v.strip())
        except ValueError:
            return v.strip()
    return v


def _calls_from_obj(obj: Any, fallback: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    """Tool calls described by a parsed JSON value (many shapes accepted)."""
    if isinstance(obj, list):
        out: List[Tuple[str, Dict[str, Any]]] = []
        for item in obj:
            out.extend(_calls_from_obj(item))
        return out
    if not isinstance(obj, dict):
        return []
    if isinstance(obj.get("tool_calls"), list):
        return _calls_from_obj(obj["tool_calls"])
    if isinstance(obj.get("function"), dict) and (obj["function"].get("name") or fallback):
        fn = obj["function"]
        return [(str(fn.get("name") or fallback), _args_to_dict(fn.get("arguments", fn.get("parameters"))))]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or obj.get("function")
    if not isinstance(name, str):
        name = ""
    for key in ("arguments", "args", "parameters", "params", "input"):
        if key in obj:
            if name or fallback:
                return [(name or fallback, _args_to_dict(obj[key]))]
            return []
    if name:
        rest = {k: v for k, v in obj.items() if k not in ("name", "tool", "tool_name", "function", "type", "id")}
        return [(name, rest)]
    if fallback:
        return [(fallback, obj)]
    return []


def _calls_from_inner(inner: str, fallback: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    inner = (inner or "").strip()
    if not inner:
        return [(fallback, {})] if fallback else []
    if _QWEN_FUNC_RE.search(inner):
        return [(m.group(1), _qwen_params(m.group(2))) for m in _QWEN_FUNC_RE.finditer(inner)]
    if re.search(rf"<\s*{_DSML}", inner, re.I):
        return _dsml_calls(inner)
    m = _INVOKE_RE.search(inner)
    if m:
        return [(m.group(1), _xml_params(m.group(2)))]
    try:
        obj = loads_lenient(inner)
    except ValueError:
        return [(fallback, {"raw": inner})] if fallback else []
    return _calls_from_obj(obj, fallback)


def _qwen_params(body: str) -> Dict[str, Any]:
    params = {m.group(1): _param_value(m.group(2)) for m in _QWEN_PARAM_RE.finditer(body)}
    if params:
        return params
    return _args_to_dict(body) if body.strip() else {}


def _xml_params(body: str) -> Dict[str, Any]:
    params = {m.group(1): _param_value(m.group(3), m.group(2)) for m in _XML_PARAM_RE.finditer(body)}
    if params:
        return params
    return _args_to_dict(body) if body.strip() else {}


def _dsml_calls(body: str) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for m in _DSML_INVOKE_RE.finditer(body):
        name, inner = m.group(1), m.group(2)
        params = {p.group(1): _param_value(p.group(3), p.group(2)) for p in _DSML_PARAM_RE.finditer(inner)}
        if not params:
            rest = _DSML_TAG_RE.sub("", inner).strip()
            params = _args_to_dict(rest) if rest else {}
        out.append((name, params))
    return out


def _schema_map(tools: Optional[List[Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if fn.get("name") and isinstance(fn.get("parameters"), dict):
            out[str(fn["name"])] = fn["parameters"]
    return out


def _coerce(value: Any, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return value
    typ = schema.get("type")
    if isinstance(typ, list):
        if isinstance(value, str) and "string" in typ:
            return value
        typ = next((t for t in typ if t != "null"), None)
    if typ == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return "true" if value else "false"
        return "" if value is None else str(value)
    if not isinstance(value, str):
        return value
    s = value.strip()
    try:
        if typ == "integer":
            return int(s)
        if typ == "number":
            f = float(s)
            return int(f) if f.is_integer() and re.fullmatch(r"-?\d+", s) else f
        if typ == "boolean":
            if s.lower() in ("true", "1", "yes"):
                return True
            if s.lower() in ("false", "0", "no"):
                return False
        if typ in ("array", "object"):
            obj = loads_lenient(s)
            if (typ == "array") == isinstance(obj, list):
                return obj
    except ValueError:
        pass
    return value


def _coerce_args(args: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    props = (schema or {}).get("properties")
    if not isinstance(props, dict) or not isinstance(args, dict):
        return args
    return {k: _coerce(v, props.get(k)) for k, v in args.items()}


def _canonical_name(name: str, requested: Optional[List[str]]) -> str:
    name = (name or "").strip().strip("\"'`")
    if not requested:
        return name
    lower = {n.lower(): n for n in requested}
    if name.lower() in lower:
        return lower[name.lower()]
    squash = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
    by_squash = {squash(n): n for n in requested}
    if squash(name) in by_squash:
        return by_squash[squash(name)]
    tail = name.split(".")[-1].split(":")[-1]
    if tail.lower() in lower:
        return lower[tail.lower()]
    for rn in requested:
        if name and (name.lower() in rn.lower() or rn.lower() in name.lower()):
            return rn
    return name


def strip_tool_markup_noise(text: str) -> str:
    """Remove stray DSML tags / model special tokens left in an answer."""
    if not text:
        return ""
    t = _DSML_TAG_RE.sub("", text)
    t = _SPECIAL_TOKEN_RE.sub("", t)
    return t


def parse_tool_calls(
    text: str,
    requested_names: Optional[List[str]] = None,
    tools: Optional[List[Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract tool calls from a web answer → (remaining text, OpenAI tool_calls)."""
    if not text:
        return "", []

    found: List[Tuple[int, int, List[Tuple[str, Dict[str, Any]]]]] = []

    def taken(pos: int) -> bool:
        return any(s <= pos < e for s, e, _ in found)

    def add(start: int, end: int, calls: List[Tuple[str, Dict[str, Any]]]) -> None:
        calls = [(n, a) for n, a in calls if n]
        if calls:
            found.append((start, end, calls))

    for m in _DSML_BLOCK_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), _dsml_calls(m.group(1)))
    for m in _DSML_INVOKE_RE.finditer(text):  # invokes written without the block
        if not taken(m.start()):
            add(m.start(), m.end(), _dsml_calls(m.group(0)))
    for m in _DSV3_CALL_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), _calls_from_inner(m.group(2), m.group(1)))
    for m in _TOOL_SUFFIX_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), _calls_from_inner(m.group(2), m.group(1)))
    for m in _TOOL_BLOCK_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), _calls_from_inner(m.group(2)))
    for m in _FUNCTION_CALL_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), _calls_from_inner(m.group(1)))
    for m in _QWEN_FUNC_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), [(m.group(1), _qwen_params(m.group(2)))])
    for m in _INVOKE_RE.finditer(text):
        if not taken(m.start()):
            add(m.start(), m.end(), [(m.group(1), _xml_params(m.group(2)))])
    # an opening <tool> never closed (answer cut / model forgot the end tag)
    for m in _TOOL_OPEN_RE.finditer(text):
        if taken(m.start()):
            continue
        nxt = _TOOL_OPEN_RE.search(text, m.end())
        end = nxt.start() if nxt else len(text)
        if any(s < end and m.start() < e for s, e, _ in found):
            continue
        inner = text[m.end():end]
        if inner.strip().startswith(("{", "[", "<function=")):
            add(m.start(), end, _calls_from_inner(inner))
    # fenced JSON naming one of the agent's tools
    if requested_names:
        known = {n.lower() for n in requested_names}
        for m in _FENCED_JSON_RE.finditer(text):
            if taken(m.start()):
                continue
            try:
                calls = _calls_from_obj(loads_lenient(m.group(2)))
            except ValueError:
                continue
            if calls and all(n.lower() in known for n, _ in calls):
                add(m.start(), m.end(), calls)

    if not found:
        if not text.strip():
            return text, []
        rest = strip_tool_markup_noise(text)
        rest = re.sub(r"\n[ \t]*(?:`{3,}|~{3,})[ \t]*(?:tool|tool_call)[ \t]*\s*\Z", "", "\n" + rest)
        return rest.strip(), []

    schemas = _schema_map(tools)
    found.sort(key=lambda x: x[0])
    tool_calls = []
    for _, _, calls in found:
        for name, args in calls:
            name = _canonical_name(name, requested_names)
            args = _coerce_args(_args_to_dict(args), schemas.get(name))
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            )

    out = []
    pos = 0
    for s, e, _ in found:
        if s > pos:
            out.append(text[pos:s])
        pos = max(pos, e)
    out.append(text[pos:])
    remaining = strip_tool_markup_noise("".join(out))
    remaining = _EMPTY_FENCE_AFTER_RE.sub("", remaining)
    remaining = re.sub(r"(?m)^[ \t]*`{1,2}[ \t]*$", "", remaining)
    # language label of a rendered code block header ("tool") left by DOM extraction
    remaining = re.sub(r"(?mi)^[ \t]*(?:tool|tool_call)[ \t]*$", "", remaining)
    # opening fence of a call joined from several parts, left alone at the end
    remaining = re.sub(r"(?m)^[ \t]*(?:`{3,}|~{3,})[ \t]*(?:tool|tool_call)?[ \t]*\s*\Z", "", remaining)
    remaining = re.sub(r"[ \t]+\n", "\n", remaining)
    remaining = re.sub(r"\n{3,}", "\n\n", remaining).strip()
    return remaining, tool_calls
