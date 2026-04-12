# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
async def activity_index(request: Request, scope: str = "project"):
    """Panel 1: Activity Stream landing page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "activity/index.html",
        {"scope": scope, "active_panel": "activity"},
    )
