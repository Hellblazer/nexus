# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI app factory for the nx console."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nexus.console.routes.health import router as health_router
from nexus.console.routes.threads import BdRunner, Clock, ThreadsReader
from nexus.console.routes.threads import router as threads_router

_PACKAGE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


def create_app(
    *,
    bd_runner: BdRunner | None = None,
    threads_reader: ThreadsReader | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Create and configure the nx console FastAPI application.

    ``bd_runner`` and ``threads_reader`` replace the Threads panel's two
    readers (the ``bd list`` call and the T2 read) and ``clock`` its notion of
    now; production passes none of them.
    """
    app = FastAPI(title="nx console", docs_url=None, redoc_url=None)
    if bd_runner is not None:
        app.state.bd_runner = bd_runner
    if threads_reader is not None:
        app.state.threads_reader = threads_reader
    if clock is not None:
        app.state.clock = clock

    # Templates
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Root redirect
    @app.get("/")
    async def root():
        return RedirectResponse(url="/threads", status_code=307)

    # Panel routers
    app.include_router(threads_router)
    app.include_router(health_router)

    return app
