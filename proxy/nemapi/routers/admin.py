"""Admin / dashboard endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter, Body, Depends

from .. import API_VERSION, __version__
from ..core.context import AppContext, get_ctx
from ..core.providers import providers_listing
from ..schemas import SettingsUpdate

router = APIRouter(tags=["Admin"])


def _settings_view(ctx: AppContext) -> dict:
    s = ctx.config.settings
    return {
        "stream_enabled": s["stream_enabled"],
        "auto_config": ctx.extension.auto_config,
        "fresh_chat": s["fresh_chat"],
        "premium_md": s["premium_md"],
    }


@router.get("/health")
async def health(ctx: AppContext = Depends(get_ctx)):
    return {"ok": True, "version": API_VERSION, "app_version": __version__, "extension": ctx.extension_connected()}


@router.get("/status")
async def status(ctx: AppContext = Depends(get_ctx)):
    connected = ctx.extension_connected()
    ext = ctx.extension
    return {
        "status": "ready" if connected else "waiting_extension",
        "version": API_VERSION,
        "app_version": __version__,
        "extension": connected,
        "uptime_seconds": int(time.time() - ctx.started_at),
        "providers_ready": ext.providers_ready(),
        "target_tabs": dict(ext.target_tabs),
        "open_tabs": [
            {"id": t.get("id"), "provider": t.get("provider"), "title": (t.get("title") or "")[:60]}
            for t in ext.tabs
        ],
        "jobs": ctx.jobs.snapshot(),
        "api_keys_enabled": ctx.config.auth_enabled,
        "settings": _settings_view(ctx),
        "integrations": ctx.integrations.summary(),
        "incremental": ctx.sessions.snapshot(),
        "last_error": ctx.logs.last_error(),
        "last_extraction": ext.last_extraction,
    }


@router.get("/stats")
async def stats(ctx: AppContext = Depends(get_ctx)):
    return ctx.stats.report(ctx.started_at, API_VERSION)


@router.post("/stats/clear")
async def stats_clear(ctx: AppContext = Depends(get_ctx)):
    ctx.stats.clear()
    return {"ok": True, "message": "Stats cleared"}


@router.get("/providers")
async def providers():
    return {"providers": providers_listing()}


@router.get("/settings")
async def get_settings(ctx: AppContext = Depends(get_ctx)):
    return _settings_view(ctx)


@router.post("/settings")
async def update_settings(body: SettingsUpdate = Body(default_factory=SettingsUpdate), ctx: AppContext = Depends(get_ctx)):
    changes = body.model_dump(include={"stream_enabled", "auto_config", "fresh_chat", "premium_md"})
    ctx.config.update_settings(changes)
    if body.auto_config is not None:
        ctx.extension.auto_config = bool(body.auto_config)
    return _settings_view(ctx)


@router.post("/stop")
async def stop(ctx: AppContext = Depends(get_ctx)):
    stopped = ctx.jobs.stop_all()
    ctx.logs.warn(f"All pending jobs stopped ({stopped} cancelled)")
    return {"ok": True, "cancelled": stopped}


@router.get("/config-full")
async def config_full(ctx: AppContext = Depends(get_ctx)):
    return {
        "api_keys": {
            "enabled": ctx.config.auth_enabled,
            "count": ctx.config.key_count,
            "keys": ctx.config.list_keys(include_secret=False),
        },
        "settings": ctx.config.settings,
    }


@router.get("/sessions")
async def sessions(ctx: AppContext = Depends(get_ctx)):
    """Incremental-context sessions (one per provider tab) and savings counters."""
    return {"enabled": ctx.integrations.incremental_enabled(), **ctx.sessions.snapshot()}


@router.post("/sessions/reset")
async def sessions_reset(ctx: AppContext = Depends(get_ctx)):
    n = ctx.sessions.reset()
    ctx.logs.info(f"Contexte incrémental: {n} session(s) réinitialisée(s)")
    return {"ok": True, "reset": n}
