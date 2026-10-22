"""Chat-completion pipeline: validate → build prompt → queue job → post-process."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..core.context import AppContext
from ..core.errors import APIError
from ..core.providers import canonical_model, resolve_provider
from ..schemas import ChatCompletionRequest
from ..utils.openai_format import approx_tokens
from ..utils.side_queries import LocalAnswer, classify_local
from ..utils.tools_format import extract_images, format_messages_prompt
from .integrations import ActiveIntegration
from .jobs import DONE, ClientGone, JobTimeout
from .sessions import Plan, postprocess


@dataclass
class PreparedCompletion:
    provider: str
    response_model: str  # echoed back to the client
    internal_model: str  # canonical model sent to the extension
    prompt: str
    tool_names: List[str]
    images: List[Dict[str, str]]
    stream: bool
    fresh_chat: Optional[bool]
    plan: Optional[Plan] = None  # incremental-context plan (feature enabled)
    tools: Optional[List[Any]] = None
    local: Optional[LocalAnswer] = None  # agent side query answered without the browser
    integration: Optional[ActiveIntegration] = None  # None = classic NemApi behaviour

    def has(self, feature: str) -> bool:
        return self.integration is not None and self.integration.has(feature)


@dataclass
class CompletionResult:
    text: str
    tool_calls: List[Dict[str, Any]]
    prompt_tokens: int
    completion_tokens: int


def prepare(ctx: AppContext, body: ChatCompletionRequest, headers: Mapping[str, str]) -> PreparedCompletion:
    model = (body.model or "").strip()
    provider = resolve_provider(model, body.provider or "")

    messages = body.messages or []
    if not messages:
        raise APIError(400, "At least one message is required.")
    if not all(isinstance(m, dict) for m in messages):
        raise APIError(400, "Each message must be an object with 'role' and 'content'.")

    tools = body.tools if body.tools is not None else body.functions
    if not isinstance(tools, list):
        tools = None

    # Classic NemApi unless the request comes from an agent whose integration
    # is enabled (« Intégration agents IA » page).
    integration = ctx.integrations.resolve(headers, messages)

    prompt = format_messages_prompt(
        messages,
        tools=tools,
        # An agent that brings its own tool instructions must not be given a
        # second, contradictory contract (Nemesis writes its own JSON dialect).
        tool_contract=not (integration is not None and integration.has("native_tool_calls")),
    )
    # Images live outside the text prompt: the extension uploads them through
    # the provider's file input before typing the prompt itself.
    try:
        images = extract_images(messages)
    except ValueError as exc:
        raise APIError(400, str(exc), "invalid_image") from exc
    if not prompt.strip() and not images:
        raise APIError(400, "Messages produced an empty prompt.")

    tool_names: List[str] = []
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            if fn.get("name"):
                tool_names.append(str(fn["name"]))

    # Explicit client flag wins; else the admin toggle. SSE Accept header forces streaming.
    if "stream" in body.model_fields_set:
        stream = bool(body.stream)
    else:
        stream = ctx.config.get("stream_enabled")
    if "text/event-stream" in (headers.get("accept") or "").lower():
        stream = True

    plan = None
    local = None
    if integration is not None:
        side = classify_local(messages, tools, body.model_extra or {})
        if side is not None and integration.has("side_queries_local"):
            local = side  # answered without the browser
        elif integration.has("incremental_context"):
            # known side queries are typed as framed side requests, never as the main conversation
            plan = ctx.sessions.plan(
                provider, messages, tools, prompt, tool_names,
                force_side=side is not None,
                generic_tools=integration.has("native_tool_calls"),
            )

    # « Discussion unique »: the request must never open a new discussion, so
    # the web chat keeps the same URL. The client cannot override it.
    fresh_chat = body.fresh_chat
    if integration is not None and integration.has("persistent_chat"):
        fresh_chat = False

    return PreparedCompletion(
        plan=plan,
        tools=tools,
        local=local,
        integration=integration,
        provider=provider,
        response_model=model or canonical_model(provider),
        internal_model=canonical_model(provider),
        prompt=prompt,
        tool_names=tool_names,
        images=images,
        stream=stream,
        fresh_chat=fresh_chat,
    )


async def ensure_ready(ctx: AppContext, provider: str) -> None:
    """Make sure the extension is online and a tab is selected for the provider."""
    if not ctx.extension_connected():
        ok = await ctx.extension.wait_connected(ctx.settings.extension_wait, ctx.jobs.has_active())
        if not ok:
            raise APIError(
                503,
                "Browser extension is not connected. Load it and open the provider tab.",
                "extension_unavailable",
            )
    if ctx.extension.tab_for(provider) is None:
        raise APIError(
            409,
            f"No {provider} tab is selected. Open http://127.0.0.1:{ctx.settings.port}/config to configure.",
            "provider_not_selected",
        )


def run_local(ctx: AppContext, prep: PreparedCompletion) -> CompletionResult:
    """Background / structured side query of the agent: answered without the browser."""
    local = prep.local
    ctx.sessions.count_local()
    if local.error:
        ctx.logs.info(f"{_agent(prep)}: {local.label} → refus local (l'agent revient à l'approbation manuelle)")
        raise APIError(400, local.error, "side_query_not_answered")
    ctx.logs.info(
        f"{_agent(prep)}: requête annexe ({local.label}) → réponse locale immédiate, "
        f"rien n'est tapé dans la discussion"
    )
    return CompletionResult(local.text, list(local.tool_calls), approx_tokens(prep.prompt), approx_tokens(local.text))


def _agent(prep: PreparedCompletion) -> str:
    return prep.integration.agent.name if prep.integration else "Agent"


async def run(ctx: AppContext, prep: PreparedCompletion, is_disconnected=None) -> CompletionResult:
    if prep.local is not None:
        return run_local(ctx, prep)
    if prep.plan is not None:
        mode = "incremental"
    else:
        mode = f"fresh={prep.fresh_chat if prep.fresh_chat is not None else ctx.config.get('fresh_chat')}"
    mode += f" mode={_agent(prep) if prep.integration else 'classique'}"
    ctx.logs.info(
        f"Completion → {prep.provider}/{prep.response_model} stream={prep.stream} "
        f"tools={len(prep.tool_names)} prompt={len(prep.prompt)}c {mode}"
    )
    job = ctx.sessions.find_inflight(prep.plan) if prep.plan is not None else None
    if job is not None:
        ctx.logs.info(f"Contexte incrémental: requête relancée → attend le job en cours {job.short_id} (pas de renvoi)")
    else:
        job = ctx.jobs.create(
            prep.prompt, prep.provider, prep.internal_model, fresh_chat=prep.fresh_chat,
            continuation=prep.has("continuation"), images=prep.images,
        )
    if prep.plan is not None and job.on_dispatch is None and job.status == "pending":
        plan = prep.plan
        job.on_dispatch = lambda j: ctx.sessions.on_dispatch(j, plan)
        job.on_complete = lambda j: ctx.sessions.on_complete(j, plan)
    tail = prep.prompt.replace("\n", " ")[-80:]
    ctx.logs.info(f"Job {job.short_id} queued → {prep.provider} ({len(prep.prompt)} chars) tail={tail!r}")

    try:
        await ctx.jobs.wait(job, ctx.settings.job_timeout, is_disconnected=is_disconnected)
    except JobTimeout:
        ctx.jobs.complete(job.id, "error", error="timeout waiting for extension")
        ctx.logs.error(f"Job {job.short_id} timed out")
        raise APIError(504, "Timeout waiting for the browser extension. Check the admin log.", "timeout")
    except ClientGone:
        raise APIError(499, "Client closed request", "client_closed")

    if job.status != DONE:
        raise APIError(500, job.error or "automation failed", "automation_error")

    parsed = job.parsed or postprocess(
        job.result, prep.tool_names, prep.tools, generic=prep.has("native_tool_calls")
    )
    # usage/cost always reflect the full conversation (what an API would bill),
    # even when only a delta was typed into the web UI
    prompt_tokens = approx_tokens(prep.prompt)
    completion_tokens = approx_tokens(parsed.text)
    ctx.stats.record(prep.provider, prompt_tokens, completion_tokens)
    return CompletionResult(parsed.text, parsed.tool_calls, prompt_tokens, completion_tokens)
