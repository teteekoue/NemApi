"""Endpoints used by the browser extension (job polling, tabs, logs, config)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request

from ..core.context import AppContext, get_ctx
from ..core.errors import APIError
from ..core.providers import PROVIDERS
from ..schemas import ExtensionLog, JobResult, TabsReport, TargetTab

router = APIRouter(tags=["Extension"])


@router.get("/job")
async def next_job(
    request: Request,
    wait: float = Query(0, ge=0, description="Long-poll: wait up to N seconds for a job (max 25)"),
    ctx: AppContext = Depends(get_ctx),
):
    ctx.extension.touch()
    wait = min(wait, ctx.settings.max_long_poll)
    ctx.extension.active_polls += 1  # a long-poll in progress counts as a heartbeat
    try:
        job = await ctx.jobs.take_wait(wait, is_disconnected=request.is_disconnected if wait else None)
    finally:
        ctx.extension.active_polls -= 1
    if job:
        ctx.extension.touch()
    if not job:
        return {"action": "idle"}
    return {
        "action": "ask",
        "jobId": job.id,
        "question": job.question,
        "provider": job.provider,
        "model": job.model,
        # per-request override wins over the global setting
        "freshChat": job.fresh_chat if job.fresh_chat is not None else ctx.config.get("fresh_chat"),
        "premiumMd": ctx.config.get("premium_md"),
        "jobTimeoutMs": int(ctx.settings.job_timeout * 1000),
        "continuation": job.parent is not None,
        # multimodal: the extension uploads these before typing the prompt
        "images": job.images or None,
    }


@router.post("/job")
async def job_result(body: JobResult, ctx: AppContext = Depends(get_ctx)):
    ctx.extension.touch()
    action = body.action if body.action in ("result", "error", "cancelled") else "error"
    accepted = ctx.jobs.complete(
        body.jobId or "", action, result=body.result or "", error=body.error or "", typed=body.typed
    )
    if action == "result":
        level, detail = "info", f"{len(body.result or '')} chars"
        if body.timer and body.timer != "worker":
            # Diagnostics of the hidden-tab fix: the content script waits on a
            # Worker when it can. "message"/"timer" means the tab is in Chrome's
            # throttled tiers (1 s / 1 min between wake-ups) — the wait loops
            # then still work, but a background tab has to be reloaded to pick
            # up the Worker (the extension was reloaded after the tab).
            ctx.logs.add(
                f"Extension timer source = {body.timer} (Worker unavailable in this tab: "
                "reload the provider tab to get unthrottled waits)",
                "warn",
            )
        if body.partial:
            ctx.logs.warn(f"Job {str(body.jobId)[:8]}: the extension stopped waiting before the answer ended")
        if body.source or body.bridge is not None:
            prev = ctx.extension.last_extraction or {}
            now = time.time()
            lossy = body.source in ("html", "dom") or body.bridge is False
            warned_at = prev.get("warned_at", 0.0) if prev.get("lossy") else 0.0
            ctx.extension.last_extraction = {
                "source": body.source, "bridge": body.bridge, "at": now, "lossy": lossy,
                "warned_at": warned_at,
            }
            detail += f", read via {body.source or 'unknown'}"
            if lossy and now - warned_at > 300:  # at most one warning every 5 min
                ctx.extension.last_extraction["warned_at"] = now
                ctx.logs.add(
                    "Answer rebuilt from the rendered page (page bridge "
                    + ("missing" if body.bridge is False else "found no markdown")
                    + "): backslashes, $ and quotes of tool calls may be altered. "
                    "Reload the provider tab (F5) after reloading the extension.",
                    "warn",
                )
    else:
        level, detail = "error", body.error or "unknown error"
    suffix = "" if accepted else " (ignored: unknown or finished job)"
    ctx.logs.add(f"Job {str(body.jobId)[:8]} {'OK' if action == 'result' else action}: {detail}{suffix}", level)
    return {"ok": True}


@router.post("/extension/log")
async def extension_log(body: ExtensionLog, ctx: AppContext = Depends(get_ctx)):
    ctx.extension.touch()
    level = body.level if body.level in ("info", "warn", "error") else "info"
    ctx.logs.add(f"Extension: {str(body.message or '')[:500]}", level)
    return {"ok": True}


@router.post("/extension/tabs")
async def extension_tabs(body: TabsReport, ctx: AppContext = Depends(get_ctx)):
    ctx.extension.update_tabs(body.tabs, body.busy)
    return {"ok": True}


@router.get("/extension/config")
async def extension_config_get(ctx: AppContext = Depends(get_ctx)):
    ctx.extension.touch()
    return {
        "targetTabs": dict(ctx.extension.target_tabs),
        "autoConfig": ctx.extension.auto_config,
        "freshChat": ctx.config.get("fresh_chat"),
        "premiumMd": ctx.config.get("premium_md"),
        # the extension sizes its waits / watchdog on the proxy timeout
        "jobTimeoutMs": int(ctx.settings.job_timeout * 1000),
    }


@router.post("/extension/config")
async def extension_config_set(body: TargetTab, ctx: AppContext = Depends(get_ctx)):
    if body.provider not in PROVIDERS or body.targetTabId is None:
        raise APIError(400, "provider and numeric targetTabId are required")
    if not ctx.extension.select_tab(body.provider, body.targetTabId):
        raise APIError(400, f"Tab {body.targetTabId} is not an open {body.provider} tab")
    return {"ok": True, "targetTabs": dict(ctx.extension.target_tabs)}


@router.get("/extension/state")
async def extension_state(ctx: AppContext = Depends(get_ctx)):
    ext = ctx.extension
    return {
        "tabs": ext.tabs,
        "targetTabs": dict(ext.target_tabs),
        "connected": ctx.extension_connected(),
        "busy": ext.busy,
        "lastSeen": ext.last_seen,
        "logs": ctx.logs.items(),
        "autoConfig": ext.auto_config,
        "lastExtraction": ext.last_extraction,
    }
