"""OpenAI-compatible API: /v1/models and /v1/chat/completions."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.context import AppContext, get_ctx
from ..core.errors import APIError
from ..core.providers import PROVIDERS, canonical_model, guess_provider
from ..core.security import require_api_key
from ..schemas import ChatCompletionRequest
from ..services import completions
from ..utils.openai_format import completion_payload, new_completion_id, sse_body, sse_events

router = APIRouter(prefix="/v1", tags=["OpenAI"], dependencies=[Depends(require_api_key)])

SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def _model_obj(ctx: AppContext, model_id: str, provider: str) -> dict:
    return {"id": model_id, "object": "model", "created": int(ctx.started_at), "owned_by": provider}


@router.get("/models")
async def list_models(ctx: AppContext = Depends(get_ctx)):
    # Only the canonical model of each provider is advertised; aliases still route.
    return {"object": "list", "data": [_model_obj(ctx, canonical_model(p), p) for p in PROVIDERS]}


@router.get("/models/{model_id:path}")
async def get_model(model_id: str, ctx: AppContext = Depends(get_ctx)):
    provider = guess_provider(model_id)
    if not provider:
        raise APIError(404, f"Model '{model_id}' not found", "not_found")
    return _model_obj(ctx, model_id, provider)


# A browser answer can take minutes (a long file written by an agent). Agents
# give up on a silent connection (Qwen Code: no response headers within 120 s,
# then no chunk within 240 s), so a streaming request that is not answered
# quickly gets its headers at once and an empty chunk every few seconds.
KEEPALIVE_AFTER = 8.0
KEEPALIVE_EVERY = 15.0


def _usage(result) -> dict:
    return {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.prompt_tokens + result.completion_tokens,
    }


async def _stream_with_keepalive(task: "asyncio.Task", chat_id: str, model: str, every: float):
    common = {"id": chat_id, "object": "chat.completion.chunk", "model": model}

    def chunk(delta: dict) -> str:
        obj = {**common, "created": int(time.time()), "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        return "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n\n"

    try:
        yield chunk({"role": "assistant"})
        while True:
            done, _ = await asyncio.wait({task}, timeout=every)
            if done:
                break
            yield chunk({"content": ""})  # keep-alive (a real chunk: resets the agent's idle timer)
        try:
            result = task.result()
        except APIError as exc:
            yield "data: " + json.dumps(
                {"error": {"message": exc.message, "type": exc.type, "code": exc.status_code}}, ensure_ascii=False
            ) + "\n\n"
            yield "data: [DONE]\n\n"
            return
        except Exception as exc:  # pragma: no cover - unexpected
            yield "data: " + json.dumps({"error": {"message": str(exc), "type": "server_error"}}) + "\n\n"
            yield "data: [DONE]\n\n"
            return
        events = sse_events(chat_id, model, result.text, result.tool_calls or None, usage=_usage(result))
        next(events, None)  # role chunk already sent
        async for part in sse_body(events):
            yield part
    finally:
        if not task.done():
            task.cancel()


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request, ctx: AppContext = Depends(get_ctx)):
    prep = completions.prepare(ctx, body, request.headers)
    if prep.local is None:
        await completions.ensure_ready(ctx, prep.provider)
    chat_id = new_completion_id()

    if prep.stream and prep.local is None and prep.has("keepalive"):
        task = asyncio.create_task(completions.run(ctx, prep, is_disconnected=request.is_disconnected))
        done, _ = await asyncio.wait({task}, timeout=KEEPALIVE_AFTER)
        if not done:
            return StreamingResponse(
                _stream_with_keepalive(task, chat_id, prep.response_model, KEEPALIVE_EVERY),
                media_type="text/event-stream; charset=utf-8",
                headers=SSE_HEADERS,
            )
        result = task.result()  # quick answer: errors keep their HTTP status
    else:
        result = await completions.run(ctx, prep, is_disconnected=request.is_disconnected)

    usage = _usage(result)
    # Clients that asked for stream:true MUST receive text/event-stream
    # (Qwen Code: "Streaming request received a non-SSE response").
    if prep.stream:
        return StreamingResponse(
            sse_body(sse_events(chat_id, prep.response_model, result.text, result.tool_calls or None, usage=usage)),
            media_type="text/event-stream; charset=utf-8",
            headers=SSE_HEADERS,
        )
    return JSONResponse(
        completion_payload(
            chat_id,
            prep.response_model,
            result.text,
            result.tool_calls or None,
            result.prompt_tokens,
            result.completion_tokens,
        )
    )
