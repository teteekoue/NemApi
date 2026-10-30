"""OpenAI **Responses API** on top of the chat pipeline (``POST /v1/responses``).

MonkeyCode's cloud agent (OhMyAgent) speaks the protocol its model is configured
with. With « OpenAI Responses » it posts ``input`` + ``instructions`` to
``/responses`` and parses a stream of ``response.*`` events; NemApi only spoke
chat completions, so the request died on a 404 and the agent reported a format
error — the user's « le format entendu ne correspond pas ».

This module translates in both directions, so the whole chat pipeline (job
queue, extension, integrations, incremental context, tool parsing) is reused
unchanged:

* ``to_chat_body`` — a Responses body becomes the chat body the pipeline takes
  (``instructions`` → system message, ``input`` items → messages, flat
  ``tools`` → chat tools, ``function_call_output`` → tool result).
* ``response_payload`` — the answer as a Responses object (non-stream).
* ``response_events`` — the SSE sequence a Responses client expects
  (``response.created`` … ``response.output_item.done`` … ``response.completed``).

Everything the request may carry and NemApi does not model (``store``,
``previous_response_id``, ``reasoning``, ``text.format``, ``include``…) is
ignored: the web chat is the state, not the API.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

_DUMPS = dict(ensure_ascii=False, separators=(",", ":"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# --------------------------------------------------------------------- input
def _text_of(content: Any) -> str:
    """The text of a Responses content value (string or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    out: List[str] = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind in ("input_text", "output_text", "text", "summary_text", ""):
            value = part.get("text")
            if isinstance(value, str):
                out.append(value)
    return "\n".join(p for p in out if p)


def _image_parts(content: Any) -> List[Dict[str, Any]]:
    """``input_image`` parts, in the shape the chat pipeline understands."""
    parts: List[Dict[str, Any]] = []
    if not isinstance(content, list):
        return parts
    for part in content:
        if not isinstance(part, dict) or str(part.get("type")) != "input_image":
            continue
        url = part.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str) and url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _message_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One ``input`` item as a chat message."""
    kind = str(item.get("type") or "message")
    role = str(item.get("role") or "user").lower()
    if role == "developer":
        role = "system"
    if kind == "function_call_output":
        output = item.get("output")
        text = output if isinstance(output, str) else _text_of(output)
        return {"role": "tool", "tool_call_id": item.get("call_id") or "", "content": text}
    if kind == "function_call":
        args = item.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, **_DUMPS)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": item.get("call_id") or item.get("id") or _new_id("call"),
                    "type": "function",
                    "function": {"name": item.get("name") or "", "arguments": args},
                }
            ],
        }
    text = _text_of(item.get("content"))
    images = _image_parts(item.get("content"))
    if images:
        content: Any = ([{"type": "text", "text": text}] if text else []) + images
    else:
        content = text
    if not text and not images:
        return None
    return {"role": role, "content": content}


def responses_input_to_messages(
    instructions: Any, items: Any
) -> List[Dict[str, Any]]:
    """``instructions`` + ``input`` of a Responses request → chat messages."""
    messages: List[Dict[str, Any]] = []
    system = _text_of(instructions) if not isinstance(instructions, str) else instructions
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    if isinstance(items, str):
        if items.strip():
            messages.append({"role": "user", "content": items})
        return messages
    if isinstance(items, dict):
        items = [items]
    for item in items or []:
        if not isinstance(item, dict):
            continue
        message = _message_from_item(item)
        if message is not None:
            messages.append(message)
    return messages


def _chat_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    """Responses tools are flat (``{type: function, name, parameters}``)."""
    if not isinstance(tools, list) or not tools:
        return None
    out: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "function") != "function":
            continue  # web_search / file_search / mcp: the web chat does not run them
        if isinstance(tool.get("function"), dict):
            out.append(tool)
            continue
        fn = {
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {"type": "object", "properties": {}},
        }
        out.append({"type": "function", "function": fn})
    return out or None


def to_chat_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """A Responses request body as the chat body the pipeline consumes."""
    requested = body.get("model")
    chat: Dict[str, Any] = {
        "model": requested,
        "messages": responses_input_to_messages(body.get("instructions"), body.get("input")),
        "stream": bool(body.get("stream")),
    }
    tools = _chat_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        chat["tool_choice"] = tool_choice
    return chat


# -------------------------------------------------------------------- output
def _usage(prompt_tokens: int, completion_tokens: int) -> Dict[str, Any]:
    return {
        "input_tokens": prompt_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _message_item(text: str, status: str = "completed") -> Dict[str, Any]:
    return {
        "type": "message",
        "id": _new_id("msg"),
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_item(call: Dict[str, Any], status: str = "completed") -> Dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    args = fn.get("arguments")
    if not isinstance(args, str):
        args = json.dumps(args or {}, **_DUMPS)
    return {
        "type": "function_call",
        "id": _new_id("fc"),
        "call_id": call.get("id") or _new_id("call"),
        "name": fn.get("name") or "",
        "arguments": args,
        "status": status,
    }


def _response_object(
    response_id: str,
    model: str,
    output: List[Dict[str, Any]],
    status: str,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": None,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "truncation": "disabled",
        "usage": usage,
    }


def response_payload(
    response_id: str,
    model: str,
    text: str,
    tool_calls: Optional[List[Dict[str, Any]]],
    prompt_tokens: int,
    completion_tokens: int,
) -> Dict[str, Any]:
    """The answer as a Responses object (``stream: false``)."""
    output: List[Dict[str, Any]] = []
    if text:
        output.append(_message_item(text))
    for call in tool_calls or []:
        output.append(_function_item(call))
    return _response_object(
        response_id,
        model,
        output,
        "completed",
        _usage(prompt_tokens, completion_tokens),
    )


def _event(name: str, payload: Dict[str, Any]) -> str:
    payload = {"type": name, **payload}
    return f"event: {name}\ndata: {json.dumps(payload, **_DUMPS)}\n\n"


def response_events(
    response_id: str,
    model: str,
    text: str,
    tool_calls: Optional[List[Dict[str, Any]]],
    prompt_tokens: int,
    completion_tokens: int,
    chunk_chars: int = 240,
) -> Iterable[str]:
    """The SSE sequence of a Responses stream.

    The browser gives the whole answer at once, so the text is sliced into
    ``output_text.delta`` events: a client that renders as it arrives (and that
    measures the first token) behaves exactly as with a real streaming API.
    """
    empty = _response_object(response_id, model, [], "in_progress")
    yield _event("response.created", {"response": empty})
    yield _event("response.in_progress", {"response": empty})

    index = 0
    if text:
        item = _message_item(text, "in_progress")
        item["content"] = []
        yield _event("response.output_item.added", {"output_index": index, "item": item})
        part = {"type": "output_text", "text": "", "annotations": []}
        yield _event(
            "response.content_part.added",
            {"item_id": item["id"], "output_index": index, "content_index": 0, "part": part},
        )
        for start in range(0, len(text), chunk_chars):
            yield _event(
                "response.output_text.delta",
                {
                    "item_id": item["id"],
                    "output_index": index,
                    "content_index": 0,
                    "delta": text[start : start + chunk_chars],
                },
            )
        yield _event(
            "response.output_text.done",
            {"item_id": item["id"], "output_index": index, "content_index": 0, "text": text},
        )
        done_part = {"type": "output_text", "text": text, "annotations": []}
        yield _event(
            "response.content_part.done",
            {"item_id": item["id"], "output_index": index, "content_index": 0, "part": done_part},
        )
        yield _event("response.output_item.done", {"output_index": index, "item": _message_item(text)})
        index += 1

    for call in tool_calls or []:
        item = _function_item(call, "in_progress")
        args = item["arguments"]
        item = {**item, "arguments": ""}
        yield _event("response.output_item.added", {"output_index": index, "item": item})
        yield _event(
            "response.function_call_arguments.delta",
            {"item_id": item["id"], "output_index": index, "delta": args},
        )
        yield _event(
            "response.function_call_arguments.done",
            {"item_id": item["id"], "output_index": index, "arguments": args},
        )
        yield _event(
            "response.output_item.done",
            {"output_index": index, "item": _function_item(call)},
        )
        index += 1

    final = response_payload(
        response_id, model, text, tool_calls, prompt_tokens, completion_tokens
    )
    yield _event("response.completed", {"response": final})


def error_payload(message: str, kind: str = "invalid_request_error", code: Any = None) -> Dict[str, Any]:
    return {"error": {"message": message, "type": kind, "code": code, "param": None}}


def error_events(message: str, kind: str = "invalid_request_error", code: Any = None) -> Iterable[str]:
    """An error in the middle of a stream, then the end of the stream."""
    yield f"event: error\ndata: {json.dumps(error_payload(message, kind, code), **_DUMPS)}\n\n"


def body_of(request_body: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Convenience: the chat body and the stream flag of a Responses request."""
    chat = to_chat_body(request_body)
    return chat, bool(chat.get("stream"))
