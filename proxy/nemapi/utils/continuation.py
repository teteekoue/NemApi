"""Answers cut off in the middle of a tool call (long files written by the agent).

A chat web UI stops an answer when the model reaches its output limit (or
when the extension gives up waiting). If that happens inside a tool call —
typically `write_file` with a long file — the lenient JSON parser used to
"repair" it (close the string, the braces) and the agent wrote a TRUNCATED
file while reporting success.

Now:
  * `find_truncated_call(text)` detects a tool call that never ended;
  * the job layer asks the model, in the same discussion, to continue exactly
    where it stopped (`continuation_prompt`), and joins the parts (`stitch`);
  * if the call still cannot be completed it is NOT executed (see
    services.sessions.postprocess) — the agent gets an explanation instead.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .tools_format import (
    _DSML,
    _DSML_BLOCK,
    _TOOL_OPEN_RE,
    _repair_json,
)

_FENCE_OPEN_RE = re.compile(r"(?m)^[ \t]*(`{3,}|~{3,})[ \t]*(tool|tool_call)?[^\n`]*$")
_DSML_PARAM_OPEN_RE = re.compile(rf"<\s*{_DSML}parameter\b[^>]*>", re.I)
_DSML_PARAM_CLOSE_RE = re.compile(rf"<\s*/\s*{_DSML}parameter\s*>", re.I)
_DSML_BLOCK_OPEN_RE = re.compile(rf"<\s*{_DSML}{_DSML_BLOCK}\s*>", re.I)
_DSML_BLOCK_CLOSE_RE = re.compile(rf"<\s*/\s*{_DSML}{_DSML_BLOCK}\s*>", re.I)

NOTE_NOT_EXECUTED = (
    "[NemApi] The answer was cut off in the middle of a tool call (output length limit of the "
    "web chat), so that incomplete call was NOT executed. Write the content in smaller parts "
    "(for example several files, or a first part then appended parts)."
)


def _json_complete(s: str) -> bool:
    """False when the JSON value stops before its end (cut inside a string / object)."""
    s = s.lstrip()
    if not s.startswith(("{", "[")):
        return True  # not JSON (qwen <function=…> etc.): do not judge
    try:
        json.JSONDecoder(strict=False).raw_decode(s)
        return True
    except ValueError:
        pass
    # same quote heuristics as the lenient parser (unescaped inner quotes are
    # not string ends): did the repair have to close a string or a brace?
    _repair_json(s)
    return not getattr(_repair_json, "auto_closed", False)


def find_truncated_call(text: str) -> Optional[int]:
    """Position (in the RAW text) of a tool call that was cut off, else None."""
    if not text:
        return None
    t = text
    # 1) <tool>{json… with no </tool> and JSON that does not end
    opens = list(_TOOL_OPEN_RE.finditer(t))
    if opens:
        m = opens[-1]
        tag = m.group(1).lower()
        rest = t[m.end():]
        if not re.search(rf"</{tag}(?::[^>]*)?\s*>", rest, re.I):
            # cut the rest at a closing fence that follows a complete JSON value
            if not _json_complete(rest):
                return m.start()
    # 2) DSML: a parameter opened and never closed, block not closed
    p_open = list(_DSML_PARAM_OPEN_RE.finditer(t))
    if p_open:
        last = p_open[-1]
        after = t[last.end():]
        if not _DSML_PARAM_CLOSE_RE.search(after) and not _DSML_BLOCK_CLOSE_RE.search(after):
            blocks = list(_DSML_BLOCK_OPEN_RE.finditer(t[: last.start()]))
            return blocks[-1].start() if blocks else last.start()
    # 3) ````tool fence holding bare JSON that never ends (model dropped <tool>)
    fences = list(_FENCE_OPEN_RE.finditer(t))
    if fences:
        f = fences[-1]
        if f.group(2):  # a "tool" fence
            body = t[f.end():]
            closing = re.search(rf"(?m)^[ \t]*{re.escape(f.group(1))}[`~]*[ \t]*$", body)
            if not closing and body.strip().startswith(("{", "[")) and not _json_complete(body):
                return f.start()
    return None


def continuation_prompt(partial: str) -> str:
    tail = partial.rstrip()[-160:]
    tail = tail.replace("````", "` ` ` `")
    return (
        "[CONTINUE] Your previous answer was cut off (output length limit) in the middle of a tool call, "
        "so nothing was executed yet. Continue it EXACTLY where it stopped: no introduction, no summary, "
        "do not restart the tool call, do not shorten the content. Begin by repeating the last line of "
        "your previous answer (at least its last 6 words) character for character, then go on. Write the "
        "continuation inside a ````tool code block (it is appended verbatim to your previous answer) and "
        "finish the tool call normally (end of the JSON, </tool>).\n"
        "Your previous answer ends with:\n"
        "````\n" + tail + "\n````"
    )


_ANY_FENCE_RE = re.compile(r"(?m)^[ \t]*(`{3,}|~{3,})[^\n]*\n([\s\S]*?)(?:\n[ \t]*\1[`~]*[ \t]*(?=\n|$)|\Z)")


def _continuation_body(cont: str) -> str:
    m = _ANY_FENCE_RE.search(cont or "")
    body = m.group(2) if m else (cont or "")
    return body.rstrip("\n")


_LEAD_IN_RE = re.compile(r"[\s.…]*")


def _join_on_overlap(p: str, body: str, max_offset: int = 40, max_overlap: int = 4000) -> Optional[str]:
    tail = p[-max_overlap:]
    for idx in range(0, min(len(body), max_offset)):
        # only a blank / "..." lead-in may come before the repeated words:
        # repetitive code would otherwise match further away and drop text
        if idx and not _LEAD_IN_RE.fullmatch(body[:idx]):
            break
        min_k = 8 if idx == 0 else 16
        seed = body[idx : idx + min_k]
        if len(seed) < min_k:
            break
        j = tail.find(seed)
        while j >= 0:  # smallest j first = longest overlap first
            if body.startswith(tail[j:], idx):
                return p + body[idx + len(tail) - j :]
            j = tail.find(seed, j + 1)
    return None


def stitch(partial: str, cont: str) -> str:
    """Join an answer cut off inside a tool call and the model's continuation."""
    body = _continuation_body(cont)
    if not body.strip():
        return partial
    pos = find_truncated_call(partial)
    stripped = body.lstrip()
    # the model restarted the whole call instead of continuing: replace it
    if pos is not None and (stripped.startswith("<tool") or re.match(r'\{\s*"name"\s*:', stripped)):
        candidate = partial[:pos] + ("" if stripped.startswith("<tool") else "<tool>") + stripped
        if find_truncated_call(candidate) is None or len(candidate) > len(partial):
            return candidate
    # overlap: the continuation repeats the end of the partial answer. The
    # repeated words are at the very start of the body (we ask for exactly
    # that), so the EARLIEST match wins: code is repetitive and a longer match
    # further away would silently drop the lines in between.
    for p in dict.fromkeys((partial, partial.rstrip())):  # trailing spaces may have been trimmed
        joined = _join_on_overlap(p, body)
        if joined is not None:
            return joined
    return partial + body


def is_truncated(text: str) -> bool:
    return find_truncated_call(text or "") is not None
