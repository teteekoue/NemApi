"""API-key authentication for the OpenAI-compatible endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request

from .context import AppContext, get_ctx
from .errors import APIError


def extract_api_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() in ("bearer", "apikey") and value.strip():
        return value.strip()
    return request.headers.get("x-api-key") or None


async def require_api_key(request: Request, ctx: AppContext = Depends(get_ctx)) -> None:
    if not ctx.config.auth_enabled:
        return
    if not ctx.config.validate_key(extract_api_key(request)):
        raise APIError(
            401,
            "Clé API requise. Ajoutez un en-tête Authorization: Bearer <votre_clé>",
            "unauthorized",
        )
