# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI app factory for the nx console."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nexus.console.routes.activity import router as activity_router
from nexus.console.routes.campaigns import router as campaigns_router
from nexus.console.routes.health import router as health_router
from nexus.console.routes.partials import router as partials_router

_PACKAGE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


def create_app() -> FastAPI:
    """Create and configure the nx console FastAPI application."""
    app = FastAPI(title="nx console", docs_url=None, redoc_url=None)

    # Templates
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Root redirect
    @app.get("/")
    async def root():
        return RedirectResponse(url="/activity", status_code=307)

    # Panel routers
    app.include_router(activity_router)
    app.include_router(health_router)
    app.include_router(campaigns_router)
    app.include_router(partials_router)

    return app
