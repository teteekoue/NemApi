"""API key management (used by the « Clés API » admin page)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from ..core.context import AppContext, get_ctx
from ..core.errors import APIError
from ..schemas import NewApiKey

router = APIRouter(prefix="/api-keys", tags=["API keys"])


@router.get("/config")
async def keys_config(ctx: AppContext = Depends(get_ctx)):
    enabled = ctx.config.auth_enabled
    return {"enabled": enabled, "require_auth": enabled, "key_count": ctx.config.key_count}


@router.get("")
async def list_keys(ctx: AppContext = Depends(get_ctx)):
    return {"api_keys": ctx.config.list_keys(include_secret=True), "require_auth": ctx.config.auth_enabled}


@router.post("")
async def create_key(body: NewApiKey = Body(default_factory=NewApiKey), ctx: AppContext = Depends(get_ctx)):
    return {"ok": True, **ctx.config.create_key(str(body.name or ""))}


@router.post("/enable")
async def enable(ctx: AppContext = Depends(get_ctx)):
    return ctx.config.enable_protection()


@router.post("/disable")
async def disable(ctx: AppContext = Depends(get_ctx)):
    return ctx.config.disable_protection()


@router.post("/restore")
async def restore(ctx: AppContext = Depends(get_ctx)):
    count = ctx.config.restore_protection()
    if count is None:
        raise APIError(404, "Aucun backup de clés trouvé", "not_found")
    return {"ok": True, "message": "Protection restaurée", "key_count": count}


@router.api_route("/{key_id}", methods=["POST", "DELETE"])
async def delete_key(key_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.config.delete_key(key_id):
        raise APIError(404, "Clé API non trouvée", "not_found")
    return {"ok": True}
