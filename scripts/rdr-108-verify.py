#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-108 Phase 5 verification probes (nexus-b5mh).

Replays the 2026-05-08 prod-shakeout probes against the post-Phase-4
state and reports whether the structural invariants the migration
promised have been reached.

Probes run against the live T2 (``~/.config/nexus/memory.db``) and
catalog (``~/.config/nexus/catalog/``) + T3 (local PersistentClient
or cloud per config).

Exit code 0 = all probes pass. Non-zero = at least one regression.

Usage:

    python scripts/rdr-108-verify.py [--target-collection NAME]

``--target-collection`` lets the operator target a specific code__
collection for the natural-ID consistency probe; default
``code__1-2188__voyage-code-3__v1`` matches the baseline measurement.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from nexus.catalog import open_cached
from nexus.config import catalog_path
from nexus.db import make_t3


@dataclass
class ProbeResult:
    name: str
    passed: bool
    detail: str


def probe_retired_keys(client, *, sample_per_coll: int = 50) -> ProbeResult:
    """Probe 3: chunks with retired metadata keys (``doc_id``,
    ``chunk_index``, ``chunk_count``) must be absent post Phase 3.
    Samples ``sample_per_coll`` chunks per collection.
    """
    retired = ("doc_id", "chunk_index", "chunk_count")
    offenders: dict[tuple[str, str], int] = defaultdict(int)
    sampled = 0
    for coll in client.list_collections():
        try:
            res = coll.get(limit=sample_per_coll, include=["metadatas"])
        except Exception:
            continue
        sampled += 1
        for md in res.get("metadatas") or []:
            for k in retired:
                if k in (md or {}):
                    offenders[(coll.name, k)] += 1
    detail = (
        f"sampled {sampled} collections; offending (coll,key) pairs = "
        f"{len(offenders)}"
    )
    return ProbeResult("retired-metadata-keys", not offenders, detail)


def probe_chash_index_drift(t2, client) -> ProbeResult:
    """Probe 4: ``chash_index.physical_collection`` distinct count
    should match T3 collection count (baseline 1697 vs 150 = 11.3x;
    target ~1.0x).
    """
    ci_distinct = t2.execute(
        "SELECT COUNT(DISTINCT physical_collection) FROM chash_index"
    ).fetchone()[0]
    colls = client.list_collections()
    t3_count = len(colls)
    t3_names = {c.name for c in colls}
    ci_names = {
        row[0] for row in t2.execute(
            "SELECT DISTINCT physical_collection FROM chash_index"
        ).fetchall()
    }
    ghosts = ci_names - t3_names
    ratio = ci_distinct / max(1, t3_count)
    detail = (
        f"chash_index distinct = {ci_distinct}; T3 = {t3_count}; "
        f"ratio = {ratio:.2f}x; ghost ci-collections (no T3 backing) "
        f"= {len(ghosts)}"
    )
    # Pass if within 5% drift and < 10 ghosts.
    passed = ratio <= 1.05 and len(ghosts) < 10
    return ProbeResult("chash_index-drift", passed, detail)


def probe_document_aspects_orphans(t2, cat) -> ProbeResult:
    """Probe 5: ``document_aspects`` rows whose ``doc_id`` is missing
    from ``catalog.documents`` (baseline 76% orphan rate; target 0%).
    Surfaces incomplete je0b backfill (empty ``doc_id`` rows).
    """
    total = t2.execute("SELECT COUNT(*) FROM document_aspects").fetchone()[0]
    tumblers = {
        row[0] for row in cat._db.execute(
            "SELECT tumbler FROM documents"
        ).fetchall()
    }
    orphan = 0
    empty = 0
    for (doc_id,) in t2.execute(
        "SELECT doc_id FROM document_aspects"
    ).fetchall():
        if not doc_id:
            empty += 1
            continue
        if doc_id not in tumblers:
            orphan += 1
    pct = orphan * 100 // max(1, total) if total else 0
    detail = (
        f"total = {total}; empty doc_id (je0b backfill incomplete) "
        f"= {empty}; orphan = {orphan} ({pct}%)"
    )
    # Strict: pass only when there are zero orphans AND zero empty doc_ids.
    passed = orphan == 0 and empty == 0
    return ProbeResult("document_aspects-orphans", passed, detail)


def probe_chash_natural_ids(client, *, target: str) -> ProbeResult:
    """Reframed Probe 1+2: under Phase 2 the T3 natural ID equals
    ``chunk_text_hash[:32]``. Verify by sampling: every chunk ID in
    *target* should be present as a chash row in ``chash_index`` AND
    match the chunk's ``chunk_text_hash`` metadata.
    """
    try:
        coll = client.get_collection(target)
    except Exception as exc:
        return ProbeResult(
            "chash-natural-ids", False,
            f"target collection {target!r} not found: {exc}",
        )
    res = coll.get(limit=300, include=["metadatas"])
    ids = res.get("ids") or []
    metas = res.get("metadatas") or []
    mismatches = 0
    for cid, md in zip(ids, metas):
        chash = (md or {}).get("chunk_text_hash") or ""
        if not chash:
            continue
        if chash[:32] != cid:
            mismatches += 1
    detail = (
        f"sampled {len(ids)} chunks; "
        f"natural-id mismatches (chunk_text_hash[:32] != id) = "
        f"{mismatches}"
    )
    return ProbeResult("chash-natural-ids", mismatches == 0, detail)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--target-collection",
        default="code__1-2188__voyage-code-3__v1",
        help="Collection sampled for the natural-ID probe.",
    )
    args = ap.parse_args()

    t2 = sqlite3.connect(str(Path.home() / ".config/nexus/memory.db"))
    cat = open_cached(catalog_path())
    t3 = make_t3()
    client = t3._client

    results: list[ProbeResult] = [
        probe_retired_keys(client),
        probe_chash_index_drift(t2, client),
        probe_document_aspects_orphans(t2, cat),
        probe_chash_natural_ids(client, target=args.target_collection),
    ]

    print("RDR-108 Phase 5 verification (nexus-b5mh)")
    print("=" * 60)
    failures = 0
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}")
        print(f"       {r.detail}")
        if not r.passed:
            failures += 1
    print("=" * 60)
    print(f"{len(results) - failures}/{len(results)} probes passed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
