# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("")
async def campaigns_index(request: Request, scope: str = "project"):
    """Panel 3: Campaigns & Provenance dashboard."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "campaigns/index.html",
        {"scope": scope, "active_panel": "campaigns"},
    )
