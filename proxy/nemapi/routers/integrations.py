"""« Intégration agents IA »: per-agent compatibility switches."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from ..core.context import AppContext, get_ctx
from ..core.errors import APIError
from ..schemas import IntegrationUpdate
from ..services.integrations import AGENTS_BY_ID

router = APIRouter(prefix="/agent-integrations", tags=["Agent integrations"])


@router.get("")
async def list_integrations(ctx: AppContext = Depends(get_ctx)):
    return {**ctx.integrations.snapshot(), "sessions": {"enabled": ctx.integrations.incremental_enabled(),
                                                      **ctx.sessions.snapshot()}}


@router.post("/{agent_id}")
async def update_integration(
    agent_id: str, body: IntegrationUpdate = Body(default_factory=IntegrationUpdate), ctx: AppContext = Depends(get_ctx)
):
    if agent_id not in AGENTS_BY_ID:
        raise APIError(404, f"Agent inconnu : {agent_id}", "not_found")
    before = ctx.integrations.incremental_enabled()
    ctx.integrations.update(agent_id, enabled=body.enabled, features=body.features)
    if ctx.integrations.incremental_enabled() != before:
        ctx.sessions.reset()  # start from a clean slate both ways
    return next(a for a in ctx.integrations.snapshot()["agents"] if a["id"] == agent_id)
