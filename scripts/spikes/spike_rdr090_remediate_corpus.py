# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-090 spike prerequisite: clean the rdr__nexus-571b8edd corpus.

Two remediation steps before re-running the spike:

1. Delete chunks whose ``source_path`` no longer exists on disk
   (renamed-RDR drift + truly-deleted prose files). 324 chunks across
   12 unique paths as of 2026-04-27.
2. Backfill structured aspects via ``nx enrich aspects`` — for
   ``rdr__*`` collections this routes to the deterministic frontmatter
   parser (``rdr-frontmatter-v1``), zero API cost.

After running, re-execute ``spike_rdr090_5q.py`` to capture clean
path A / B / C numbers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

COLLECTION = "rdr__nexus-571b8edd"


def stale_source_paths(t3, collection: str) -> dict[str, int]:
    """Enumerate ``source_path`` values whose file no longer exists.

    Returns ``{source_path: chunk_count}`` sorted by count desc.
    """
    coll = t3._client.get_collection(collection)
    seen: Counter[str] = Counter()
    offset = 0
    while True:
        res = coll.get(limit=300, offset=offset, include=["metadatas"])
        metas = res.get("metadatas") or []
        if not metas:
            break
        for m in metas:
            sp = (m or {}).get("source_path", "") or ""
            if sp and not os.path.exists(sp):
                seen[sp] += 1
        offset += 300
        if len(metas) < 300:
            break
    return dict(seen)


def delete_stale(t3, collection: str, dry_run: bool = False) -> tuple[int, int]:
    """Delete every stale chunk in ``collection``.

    Returns ``(unique_paths_deleted, chunks_deleted)``.
    """
    stale = stale_source_paths(t3, collection)
    if not stale:
        print(f"  no stale chunks in {collection}")
        return (0, 0)
    total_paths = 0
    total_chunks = 0
    for sp, expected_count in sorted(stale.items(), key=lambda x: -x[1]):
        base = os.path.basename(sp)
        prefix = "[DRY-RUN]" if dry_run else "[DELETE ]"
        if dry_run:
            actual = expected_count
        else:
            actual = t3.delete_by_source(collection, sp)
        print(f"  {prefix} {actual:4d} chunks  {base[:55]}")
        total_paths += 1
        total_chunks += actual
    return (total_paths, total_chunks)


def backfill_aspects(collection: str) -> int:
    """Run ``nx enrich aspects <collection>``. Returns subprocess exit code."""
    print(f"\n=== Aspect backfill: nx enrich aspects {collection} ===")
    result = subprocess.run(
        ["nx", "enrich", "aspects", collection, "--validate-sample", "0"],
        text=True,
        timeout=1800,
    )
    return result.returncode


def main() -> int:
    from nexus.mcp_infra import get_t3
    from nexus.db.t2 import T2Database
    from nexus.commands._helpers import default_db_path

    dry_run = "--dry-run" in sys.argv
    skip_aspects = "--no-aspects" in sys.argv

    t3 = get_t3()
    t2 = T2Database(default_db_path())

    print(f"=== Step 1: stale source_path cleanup on {COLLECTION} ===")
    info_before = t3.collection_info(COLLECTION)
    print(f"  Pre-cleanup: {info_before['count']} chunks")
    paths, chunks = delete_stale(t3, COLLECTION, dry_run=dry_run)
    if dry_run:
        print(f"\n[DRY-RUN] would delete {chunks} chunks across {paths} stale paths")
        return 0
    info_after = t3.collection_info(COLLECTION)
    print(f"\n  Post-cleanup: {info_after['count']} chunks ({paths} paths, {chunks} chunks deleted)")

    aspects_before = len(t2.document_aspects.list_by_collection(COLLECTION))
    print(f"\n  Aspects before backfill: {aspects_before}")

    if skip_aspects:
        print("  (skipping aspect backfill — --no-aspects)")
        return 0

    rc = backfill_aspects(COLLECTION)
    if rc != 0:
        print(f"  aspect backfill exit={rc}")
        return rc

    aspects_after = len(t2.document_aspects.list_by_collection(COLLECTION))
    print(f"\n  Aspects after backfill: {aspects_after} (+{aspects_after - aspects_before})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
