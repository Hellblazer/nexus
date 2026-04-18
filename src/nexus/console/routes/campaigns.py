# SPDX-License-Identifier: AGPL-3.0-or-later
"""Panel 3: Campaigns & Provenance — agent campaign tracking."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

SYSTEM_CREATORS = frozenset({"index_hook", "filepath_extractor", "auto-linker"})


def _catalog_dir() -> Path:
    # Delegate to the canonical resolver so NEXUS_CONFIG_DIR and
    # NEXUS_CATALOG_PATH redirections land consistently.
    from nexus.config import catalog_path

    return catalog_path()


def _load_campaign_summary(links_path: Path) -> dict[str, dict[str, Any]]:
    """One-pass groupby over links.jsonl → {created_by: summary}."""
    if not links_path.exists():
        return {}

    campaigns: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "link_types": set(),
        "first_seen": "",
        "last_seen": "",
    })

    try:
        for line in links_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("_deleted"):
                continue

            creator = record.get("created_by", "unknown")
            ts = record.get("created_at", "")
            c = campaigns[creator]
            c["count"] += 1
            c["link_types"].add(record.get("link_type", ""))
            if not c["first_seen"] or ts < c["first_seen"]:
                c["first_seen"] = ts
            if not c["last_seen"] or ts > c["last_seen"]:
                c["last_seen"] = ts
    except OSError:
        return {}

    # Convert sets to sorted lists for template rendering
    result = {}
    for creator, data in campaigns.items():
        result[creator] = {
            "count": data["count"],
            "link_types": sorted(data["link_types"] - {""}),
            "first_seen": data["first_seen"][:19],
            "last_seen": data["last_seen"][:19],
        }

    return result


@router.get("")
async def campaigns_index(request: Request, scope: str = "project"):
    """Panel 3: Campaigns & Provenance landing page."""
    links_path = _catalog_dir() / "links.jsonl"
    all_campaigns = _load_campaign_summary(links_path)

    system = {k: v for k, v in all_campaigns.items() if k in SYSTEM_CREATORS}
    named = {k: v for k, v in all_campaigns.items() if k not in SYSTEM_CREATORS}

    # Sort by count descending
    system = dict(sorted(system.items(), key=lambda x: x[1]["count"], reverse=True))
    named = dict(sorted(named.items(), key=lambda x: x[1]["count"], reverse=True))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "campaigns/index.html",
        {
            "scope": scope,
            "active_panel": "campaigns",
            "system_campaigns": system,
            "named_campaigns": named,
        },
    )


@router.get("/{created_by}")
async def campaign_detail(request: Request, created_by: str, scope: str = "project"):
    """Campaign detail — links and docs for a specific creator."""
    links_path = _catalog_dir() / "links.jsonl"
    docs_path = _catalog_dir() / "documents.jsonl"

    # Collect links by this creator
    links: list[dict[str, Any]] = []
    if links_path.exists():
        try:
            for line in links_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("_deleted"):
                    continue
                if record.get("created_by") == created_by:
                    links.append(record)
        except OSError:
            pass

    # Collect docs by this creator
    docs: list[dict[str, Any]] = []
    if docs_path.exists():
        try:
            for line in docs_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("_deleted"):
                    continue
                if record.get("created_by") == created_by:
                    docs.append(record)
        except OSError:
            pass

    # Group links by type
    by_type: dict[str, list[dict]] = defaultdict(list)
    for link in links:
        by_type[link.get("link_type", "unknown")].append(link)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "campaigns/detail.html",
        {
            "scope": scope,
            "active_panel": "campaigns",
            "created_by": created_by,
            "links": links,
            "docs": docs,
            "links_by_type": dict(by_type),
            "total_links": len(links),
            "total_docs": len(docs),
        },
    )
