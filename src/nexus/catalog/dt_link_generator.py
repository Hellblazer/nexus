# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DEVONthink semantic + structural link generation (RDR-139 Layer B).

A new generator alongside :mod:`nexus.catalog.link_generator` and
:mod:`nexus.catalog.auto_linker` — it does NOT modify them. Given a just-indexed
record's tumbler and its DEVONthink UUID, it pulls two neighbour sources off the
DT MCP server and writes ``relates`` edges into the catalog:

- **Similarity** (``dt_find_similar``, DT 'See Also') → ``created_by='dt_similar'``.
- **Explicit links** (``dt_record_links``, author-curated item links, higher
  precision) → ``created_by='dt_link'``, deduped against similarity edges so a
  pair already joined by similarity is not re-counted.

Every neighbour UUID is mapped through ``Catalog.by_source_uri`` — neighbours
that are not indexed in nexus resolve to ``None`` and are silently skipped. The
whole layer is gated on :func:`devonthink.available`: DT down → zero new edges,
exit clean (Gap 0). ``classify_record`` proposals are advisory-only (logged,
no edges) since they suggest groups, not record-to-record links.
"""

from __future__ import annotations

import structlog

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.mcp_client import devonthink as _devonthink

log = structlog.get_logger(__name__)

#: Default similarity floor for ``dt_find_similar`` (live DT scores ~0.5-0.6).
DEFAULT_SIMILARITY_FLOOR = 0.5

#: Default per-source neighbour cap.
DEFAULT_LIMIT = 25


def _dt_uri(uuid: str) -> str:
    return f"x-devonthink-item://{uuid}"


def generate_dt_links(
    cat: Catalog,
    this: Tumbler,
    dt_uuid: str,
    *,
    floor: float = DEFAULT_SIMILARITY_FLOOR,
    limit: int = DEFAULT_LIMIT,
    classify: bool = False,
    dt_client=_devonthink,
) -> dict[str, int]:
    """Generate DT-derived ``relates`` edges for one record.

    Returns counts ``{"similar": n, "link": m}`` of genuinely-new edges. When DT
    is unavailable returns zeros and logs a single skip line — the tested
    fallback for this layer (zero new edges).

    ``dt_client`` is injectable for testing; it defaults to the real
    :mod:`nexus.mcp_client.devonthink` module.
    """
    counts = {"similar": 0, "link": 0}
    if not dt_client.available():
        log.info("dt_link_skipped_unavailable", tumbler=str(this), dt_uuid=dt_uuid)
        return counts

    linked: set[Tumbler] = set()

    # Similarity neighbours (DT 'See Also').
    for n in dt_client.dt_find_similar(dt_uuid, limit=limit, floor=floor):
        entry = cat.by_source_uri(_dt_uri(n["uuid"]))
        if entry is None or entry.tumbler == this or entry.tumbler in linked:
            continue
        if cat.link_if_absent(this, entry.tumbler, "relates", created_by="dt_similar"):
            counts["similar"] += 1
        linked.add(entry.tumbler)

    # Explicit DT item links (higher precision); dedup against similarity edges.
    for n in dt_client.dt_record_links(dt_uuid):
        entry = cat.by_source_uri(_dt_uri(n["uuid"]))
        if entry is None or entry.tumbler == this or entry.tumbler in linked:
            continue
        if cat.link_if_absent(this, entry.tumbler, "relates", created_by="dt_link"):
            counts["link"] += 1
        linked.add(entry.tumbler)

    if classify:
        proposals = dt_client.dt_call("classify_record", {"uuid": dt_uuid}) or {}
        log.info(
            "dt_classify_advisory",
            tumbler=str(this),
            dt_uuid=dt_uuid,
            proposal_count=len(proposals) if isinstance(proposals, dict) else 0,
        )

    log.info(
        "dt_links_generated",
        tumbler=str(this),
        dt_uuid=dt_uuid,
        similar=counts["similar"],
        link=counts["link"],
    )
    return counts
