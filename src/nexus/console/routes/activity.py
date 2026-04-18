# SPDX-License-Identifier: AGPL-3.0-or-later
"""Panel 1: Activity Stream — live feed of catalog events."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["activity"])


def _catalog_dir() -> Path:
    """Return the catalog directory path."""
    import os

    override = os.environ.get("NEXUS_CATALOG_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus" / "catalog"


def _read_recent_events(
    limit: int = 100,
    scope: str = "project",
    created_by: str | None = None,
    content_type: str | None = None,
    link_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read recent events from links.jsonl and documents.jsonl, merged and sorted."""
    events: list[dict[str, Any]] = []
    cat_dir = _catalog_dir()

    for fname, kind in [("links.jsonl", "link"), ("documents.jsonl", "document")]:
        path = cat_dir / fname
        if not path.exists():
            continue
        try:
            lines = path.read_text().splitlines()
            # Read from the end for efficiency
            for line in reversed(lines):
                if len(events) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("_deleted"):
                    continue
                record["_event_type"] = kind

                # Apply filters
                if created_by and record.get("created_by") != created_by:
                    continue
                if kind == "link" and link_type and record.get("link_type") != link_type:
                    continue
                if kind == "document" and content_type and record.get("content_type") != content_type:
                    continue

                events.append(record)
        except OSError:
            continue

    # Sort by timestamp descending
    def _ts(e: dict) -> str:
        return e.get("created_at") or e.get("indexed_at") or ""

    events.sort(key=_ts, reverse=True)
    return events[:limit]


# Whitelist of kind values safe to emit into the CSS ``class`` attribute
# of ``_stream.html``. Any other value is silently coerced to ``"event"``
# to keep a future edit that threads untrusted input through this function
# from producing attribute injection (CLI review Critical — defensive).
_ALLOWED_KINDS: frozenset[str] = frozenset({"link", "document", "event"})


def _safe_kind(value: str) -> str:
    return value if value in _ALLOWED_KINDS else "event"


def _event_summary(event: dict[str, Any]) -> dict[str, str]:
    """Extract display fields from a raw event.

    Every returned ``kind`` is drawn from ``_ALLOWED_KINDS`` so the
    template's ``<tr class="event-row {{ e.kind }}">`` cannot be used
    to inject arbitrary attributes even if the JSONL source is
    corrupted or written by an external tool.
    """
    kind = event.get("_event_type", "")
    if kind == "link":
        return {
            "timestamp": event.get("created_at", ""),
            "actor": event.get("created_by", ""),
            "action": event.get("link_type", "link"),
            "target": f"{event.get('from_t', '')} → {event.get('to_t', '')}",
            "kind": _safe_kind("link"),
        }
    return {
        "timestamp": event.get("indexed_at", ""),
        "actor": event.get("created_by", "") or "indexer",
        "action": f"register {event.get('content_type', '')}",
        "target": event.get("title", event.get("tumbler", "")),
        "kind": _safe_kind("document"),
    }


@router.get("/activity")
async def activity_index(request: Request, scope: str = "project"):
    """Panel 1: Activity Stream landing page."""
    events = _read_recent_events(limit=100, scope=scope)
    summaries = [_event_summary(e) for e in events]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "activity/index.html",
        {"scope": scope, "active_panel": "activity", "events": summaries},
    )


@router.get("/activity/events")
async def activity_events(
    request: Request,
    scope: str = "project",
    created_by: str | None = None,
    content_type: str | None = None,
    link_type: str | None = None,
    limit: int = 100,
):
    """Paginated event list — returns HTMX partial."""
    events = _read_recent_events(
        limit=limit, scope=scope, created_by=created_by,
        content_type=content_type, link_type=link_type,
    )
    summaries = [_event_summary(e) for e in events]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "activity/_stream.html",
        {"events": summaries},
    )


@router.get("/activity/event/{event_id}")
async def activity_event_detail(request: Request, event_id: str):
    """Event detail right-panel partial."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "activity/_detail.html",
        {"event_id": event_id, "event": None},
    )
