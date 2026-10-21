"""Builders for OpenAI-compatible chat completion payloads (JSON + SSE)."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

_DUMPS = dict(ensure_ascii=False, separators=(",", ":"))


def approx_tokens(value: str) -> int:
    return max(1, len(value or "") // 4)


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def completion_payload(
    chat_id: str,
    model: str,
    text: str,
    tool_calls: Optional[List[Dict[str, Any]]],
    prompt_tokens: int,
    completion_tokens: int,
) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": text if text else (None if tool_calls else ""),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _event(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, **_DUMPS)}\n\n"


def sse_events(
    chat_id: str,
    model: str,
    text: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    usage: Optional[Dict[str, int]] = None,
    text_chunk: int = 64,
    args_chunk: int = 80,
) -> Iterator[str]:
    """Yield OpenAI `chat.completion.chunk` SSE events, then `data: [DONE]`."""
    common = {"id": chat_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}

    def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> str:
        return _event({**common, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    yield chunk({"role": "assistant"})

    for offset in range(0, len(text or ""), text_chunk):
        yield chunk({"content": text[offset : offset + text_chunk]})

    for i, tc in enumerate(tool_calls or []):
        fn = tc.get("function") or {}
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": i,
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {"name": fn.get("name") or "", "arguments": ""},
                    }
                ]
            }
        )
        args = fn.get("arguments") or "{}"
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        for offset in range(0, len(args), args_chunk):
            yield chunk({"tool_calls": [{"index": i, "function": {"arguments": args[offset : offset + args_chunk]}}]})

    final = {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_calls else "stop"}]}
    if usage:
        final["usage"] = usage
    yield _event(final)
    yield "data: [DONE]\n\n"


async def sse_body(events: Iterator[str], batch_bytes: int = 16384):
    """Async wrapper grouping SSE events into ~16 KB writes.

    Starlette iterates *sync* generators in a thread pool (one hop per chunk);
    an async generator with batching avoids hundreds of thread hops for long
    answers. Each event stays a complete `data: …\\n\\n` frame.
    """
    buf: list = []
    size = 0
    for ev in events:
        buf.append(ev)
        size += len(ev)
        if size >= batch_bytes:
            yield "".join(buf)
            buf, size = [], 0
    if buf:
        yield "".join(buf)
