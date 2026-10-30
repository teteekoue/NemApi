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
    tool_mode: str = "openai"  # "openai" | "native" | "off" (tool-call dialect)
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
    provider = resolve_provider(model)

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
    integration = ctx.integrations.resolve(headers, messages, provider, tools)

    # Which tool-call dialect applies:
    #   "openai" — the agent sends the standard OpenAI "tools" array; NemApi
    #              gives the model its contract (the web model needs it to
    #              write a parsable call) and validates the names it answers.
    #   "native" — the agent brings its own dialect (Nemesis writes JSON);
    #              NemApi must not inject a second, contradictory contract.
    #   "off"    — the integration does not want tool calls at all.
    if integration is None:
        tool_mode = "openai"
    elif integration.has("native_tool_calls"):
        tool_mode = "native"
    elif integration.has("tool_calls"):
        tool_mode = "openai"
    else:
        tool_mode = "off"
    if tool_mode == "off":
        tools = None

    prompt = format_messages_prompt(
        messages,
        tools=tools,
        tool_contract=(tool_mode == "openai"),
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

    # THE CLIENT decides, as the OpenAI contract says:
    #   stream:true in the body, or `Accept: text/event-stream`  → SSE
    #   anything else                                            → JSON
    #
    # The admin « Streaming » toggle used to force SSE on clients that asked
    # for nothing. That broke every spec-compliant client whose parser expects
    # JSON: MonkeyCode's health check (Go, `json.Unmarshal`) fails on the
    # `d` of `data: ` with "Invalid character 'd' looking for beginning of
    # value", and a request that would have worked could not even be saved.
    # The toggle now means "streaming allowed": turned off, a client that asks
    # for a stream gets a clear 400 instead of a silently non-SSE answer.
    stream = bool(body.stream) if "stream" in body.model_fields_set else False
    if "text/event-stream" in (headers.get("accept") or "").lower():
        stream = True
    if stream and not ctx.config.get("stream_enabled"):
        raise APIError(
            400,
            "Streaming is disabled in NemApi (Configuration → Streaming). "
            "Send `stream: false`, or turn the switch back on.",
            "streaming_disabled",
        )

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
    # the web chat keeps the same URL. The client cannot decide it: the proxy
    # does — the global setting, overridden by the agent integration.
    fresh_chat = None
    if integration is not None and (
        integration.has("persistent_chat") or integration.has("incremental_context")
    ):
        fresh_chat = False

    return PreparedCompletion(
        plan=plan,
        tools=tools,
        tool_mode=tool_mode,
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
    # A 100 KB prompt typed in full is slow and fragile (the site's composer
    # has its limits, and an editor that truncates it can never be confirmed).
    # The agent integration types only what is new: say it, once per request.
    if prep.integration is None and len(prep.prompt) > 60000:
        ctx.logs.warn(
            f"Prompt de {len(prep.prompt)}c tapé en entier (mode classique) : "
            "active « Intégration agents IA » pour cet agent — le contexte incrémental "
            "ne tape que le message nouveau"
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
        job.result, prep.tool_names, prep.tools, generic=(prep.tool_mode == "native")
    )
    # usage/cost always reflect the full conversation (what an API would bill),
    # even when only a delta was typed into the web UI
    prompt_tokens = approx_tokens(prep.prompt)
    completion_tokens = approx_tokens(parsed.text)
    ctx.stats.record(prep.provider, prompt_tokens, completion_tokens)
    return CompletionResult(parsed.text, parsed.tool_calls, prompt_tokens, completion_tokens)
