"""HTML pages of the admin web UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from ..core.context import AppContext, get_ctx
from ..core.errors import APIError

router = APIRouter(include_in_schema=False)

PAGES = {
    "/": "admin.html",
    "/index.html": "admin.html",
    "/admin.html": "admin.html",
    "/config": "config.html",
    "/integrations": "integrations.html",
    "/chat": "chat.html",
    "/analytics": "analytics.html",
    "/api-keys-page": "api-keys.html",
}


def _page_endpoint(filename: str):
    async def endpoint(ctx: AppContext = Depends(get_ctx)):
        path = ctx.settings.web_dir / filename
        if not path.is_file():
            raise APIError(404, f"Page unavailable: {filename}", "not_found")
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    return endpoint


for _route, _file in PAGES.items():
    router.add_api_route(_route, _page_endpoint(_file), methods=["GET"])


@router.get("/extension/icons/{name}")
async def legacy_icons(name: str, ctx: AppContext = Depends(get_ctx)):
    """Backward compat: v4.0 pages loaded logos from /extension/icons/."""
    path = (ctx.settings.web_dir / "icons" / name).resolve()
    if path.parent != (ctx.settings.web_dir / "icons").resolve() or not path.is_file():
        raise APIError(404, "not found", "not_found")
    return FileResponse(path)
