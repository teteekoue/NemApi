"""FastAPI application factory."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .core.context import AppContext
from .core.errors import install_error_handlers
from .core.settings import Settings
from .routers import admin, api_keys, extension, integrations, openai, pages


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = AppContext.build(settings)
        app.state.ctx = ctx
        ctx.stats.start()
        # Safety net for jobs nobody waits for any more (client gone after dispatch).
        reaper = asyncio.create_task(ctx.jobs.reaper_loop(settings.job_timeout + 5, interval=min(5.0, settings.job_timeout)))
        ctx.logs.info(f"NemApi {__version__} prêt sur http://{settings.host}:{settings.port}/")
        try:
            yield
        finally:
            reaper.cancel()
            ctx.jobs.stop_all()
            await ctx.stats.stop()

    app = FastAPI(
        title="NemApi",
        version=__version__,
        description="Passerelle locale compatible OpenAI pilotant les interfaces web des LLM via une extension Chromium.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    install_error_handlers(app)

    app.include_router(openai.router)
    app.include_router(extension.router)
    app.include_router(admin.router)
    app.include_router(api_keys.router)
    app.include_router(integrations.router)
    app.include_router(pages.router)
    # Static assets of the web UI (admin.css, admin.js, icons/…) — mounted last.
    app.mount("/", StaticFiles(directory=settings.web_dir, check_dir=False), name="web")
    return app
