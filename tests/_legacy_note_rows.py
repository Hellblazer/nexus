# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chunk row a note's put left before RDR-236 Phase 4 Step 1 (nexus-6pfgb.65).

A client from before Step 1 stamped the note's own facts on its chunk rows: the writer's tumbler
(``catalog_doc_id``), the title, tags, category, TTL, agent and write time. A current client writes the CHUNK
keys only. The tests that cover the legacy rules (the note TTL sweep's row-written-by attribution, the recovery
bundle's keyless-note recovery, the display fallbacks) need rows of the old shape, which only an old client could
write: :func:`stamp_legacy_note_rows` puts them on, through the engine's metadata merge, as the old write did.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Sequence


def stamp_legacy_note_rows(
    client: Any, collection: str, chunk_ids: Sequence[str], *, catalog_doc_id: str, title: str,
    tags: str = "", category: str = "", ttl_days: int | None = None, source_agent: str = "",
    indexed_at: str | None = None,
) -> None:
    """Merge the keys a pre-Step-1 note put wrote onto each of *chunk_ids*."""
    meta = {
        "catalog_doc_id": catalog_doc_id, "title": title, "tags": tags, "category": category,
        "ttl_days": ttl_days, "source_agent": source_agent,
        "indexed_at": indexed_at or datetime.now(UTC).isoformat(),
    }
    ids = list(chunk_ids)
    client.update_chunks(collection, ids, [dict(meta) for _ in ids])
