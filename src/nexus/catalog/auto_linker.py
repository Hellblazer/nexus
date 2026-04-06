# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Auto-create catalog links from T1 scratch link-context entries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger()


@dataclass(frozen=True)
class LinkContext:
    """A single link target parsed from a T1 scratch link-context entry."""

    target_tumbler: str
    link_type: str


def read_link_contexts(entries: list[dict[str, Any]]) -> list[LinkContext]:
    """Parse T1 scratch entries into LinkContext objects.

    Each entry may contain a ``targets`` array with multiple items.
    Flattens: one entry with N targets → N LinkContext objects.
    """
    contexts: list[LinkContext] = []
    for entry in entries:
        raw = entry
        # If the entry has a "content" key (raw T1 scratch format), parse it
        if isinstance(raw.get("content"), str):
            try:
                raw = json.loads(raw["content"])
            except (json.JSONDecodeError, TypeError):
                continue

        targets = raw.get("targets", [])
        for item in targets:
            tumbler = item.get("target_tumbler") or item.get("tumbler", "")
            link_type = item.get("link_type", "relates")
            if tumbler:
                contexts.append(LinkContext(target_tumbler=tumbler, link_type=link_type))
    return contexts


def auto_link(
    cat: Catalog,
    source_tumbler: Tumbler,
    contexts: list[LinkContext],
) -> int:
    """Create catalog links from source to each target in contexts.

    Uses ``link_if_absent`` for idempotency. Skips targets whose tumblers
    don't resolve. Returns the number of links actually created.
    """
    if not contexts:
        return 0

    count = 0
    for ctx in contexts:
        try:
            target = Tumbler.parse(ctx.target_tumbler)
        except ValueError:
            _log.debug("auto_link_skip_invalid_tumbler", tumbler=ctx.target_tumbler)
            continue

        try:
            created = cat.link_if_absent(
                source_tumbler,
                target,
                ctx.link_type,
                created_by="auto-linker",
            )
        except ValueError:
            # Endpoint not found in catalog — skip gracefully
            _log.debug(
                "auto_link_skip_missing_endpoint",
                source=str(source_tumbler),
                target=str(target),
            )
            continue

        if created:
            count += 1
            _log.debug(
                "auto_link_created",
                source=str(source_tumbler),
                target=str(target),
                link_type=ctx.link_type,
            )

    return count
