# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_index(request: Request, scope: str = "project"):
    """Panel 2: Sessions & Health dashboard."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "health/index.html",
        {"scope": scope, "active_panel": "health"},
    )
