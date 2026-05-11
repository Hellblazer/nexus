#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill ``document_aspects.doc_id`` for rows that the je0b PK
migration left empty (nexus-f8u8).

The 2026-05-11 RDR-108 Phase 5 verification (``scripts/rdr-108-verify.py``
PROBE 5) surfaced 329 rows with NULL or empty ``doc_id``. These rows
were written under the legacy ``(collection, source_path)`` PK and were
not retroactively populated when je0b switched the PK to ``doc_id``.

The catalog has since promoted RDR-103 conformant collection names
(e.g. ``rdr__nexus-571b8edd`` -> ``rdr__1-2188__voyage-context-3__v1``),
so a direct ``physical_collection = ?`` join misses. This script
resolves doc_id via a wider catalog lookup:

1. Exact ``file_path = source_path`` match anywhere in the catalog.
2. ``source_uri = source_uri`` match anywhere in the catalog.
3. Suffix match: ``source_path`` ends with ``file_path`` (handles the
   relative-vs-absolute path skew).
4. If multiple candidates survive, prefer the row whose
   ``physical_collection`` shares the content_type prefix (``rdr__``,
   ``docs__``, etc.) of the aspect row's stored ``collection``.

Dry-run by default. ``--apply`` writes. Backs up the pre-write state
to ``~/.config/nexus/backup/document_aspects-je0b-backfill-<ts>.json``
before any UPDATE.

Usage:

    python scripts/rdr-108-je0b-backfill.py            # dry-run
    python scripts/rdr-108-je0b-backfill.py --apply    # commit
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from nexus.catalog import open_cached
from nexus.config import catalog_path, nexus_config_dir


def _resolve(
    cat,
    aspect_collection: str,
    source_path: str,
    source_uri: str | None,
) -> tuple[str | None, str]:
    """Return (resolved_doc_id, reason_or_strategy).

    Multi-match tiebreaker: among candidates with the same
    content_type prefix as the aspect row's stored collection, pick
    the one with the most-recent ``indexed_at``. The catalog has
    accumulated duplicate Document rows per re-index (every reindex
    writes a new row instead of updating in place), so the youngest
    row is the live one that the indexer's next pass will touch.
    """
    cols = "tumbler, physical_collection, file_path, indexed_at"
    rows = cat._db.execute(
        f"SELECT {cols} FROM documents WHERE file_path = ?",
        (source_path,),
    ).fetchall()

    if not rows and source_uri:
        rows = cat._db.execute(
            f"SELECT {cols} FROM documents WHERE source_uri = ?",
            (source_uri,),
        ).fetchall()

    if not rows:
        candidate = source_path.lstrip("/")
        rows = cat._db.execute(
            f"SELECT {cols} FROM documents WHERE file_path LIKE ?",
            (f"%{candidate}",),
        ).fetchall()
        rows = [r for r in rows if source_path.endswith(r[2])]

    if not rows:
        return None, "no_catalog_match"

    if len(rows) == 1:
        return rows[0][0], "unique"

    prefix = aspect_collection.split("__", 1)[0] + "__"
    same_prefix = [r for r in rows if r[1].startswith(prefix)]
    pool = same_prefix or rows
    # Sort by indexed_at descending — pick the youngest live row.
    pool_sorted = sorted(pool, key=lambda r: r[3] or "", reverse=True)
    chosen = pool_sorted[0]
    return chosen[0], (
        "youngest_prefix" if same_prefix else "youngest_any_prefix"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true",
        help="Commit updates (default is dry-run).",
    )
    ap.add_argument(
        "--delete-unresolvable", action="store_true",
        help=(
            "Delete document_aspects rows that no catalog Document "
            "matches. Use only after a dry-run inspection; these rows "
            "point at deleted/never-cataloged documents."
        ),
    )
    args = ap.parse_args()

    t2_path = nexus_config_dir() / "memory.db"
    t2 = sqlite3.connect(str(t2_path))
    cat = open_cached(catalog_path())

    rows = t2.execute(
        "SELECT rowid, collection, source_path, source_uri "
        "FROM document_aspects "
        "WHERE doc_id IS NULL OR doc_id = ''"
    ).fetchall()
    print(f"empty-doc_id rows: {len(rows)}")

    resolutions: list[tuple[int, str | None, str]] = []
    reasons: dict[str, int] = defaultdict(int)
    for rowid, coll, sp, su in rows:
        resolved, reason = _resolve(cat, coll, sp, su)
        resolutions.append((rowid, resolved, reason))
        reasons[reason if resolved else f"unresolved:{reason}"] += 1

    print("resolution summary:")
    for reason, n in sorted(reasons.items(), key=lambda p: -p[1]):
        print(f"  {reason:>30}  {n:>4}")

    resolved_count = sum(1 for _, did, _ in resolutions if did)
    print(f"\nresolvable: {resolved_count}/{len(rows)}")

    if not args.apply:
        print("\n(dry-run; pass --apply to commit)")
        return 0

    # Snapshot before write.
    backup_dir = nexus_config_dir() / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = (
        backup_dir / f"document_aspects-je0b-backfill-{ts}.json"
    )
    pre_state = [
        {"rowid": rid, "collection": c, "source_path": sp,
         "source_uri": su, "doc_id_before": ""}
        for rid, c, sp, su in rows
    ]
    backup_path.write_text(json.dumps(pre_state, indent=2))
    print(f"backup: {backup_path}")

    # Write.
    updated = 0
    deleted = 0
    for rowid, resolved, _ in resolutions:
        if resolved:
            t2.execute(
                "UPDATE document_aspects SET doc_id = ? WHERE rowid = ?",
                (resolved, rowid),
            )
            updated += 1
        elif args.delete_unresolvable:
            t2.execute("DELETE FROM document_aspects WHERE rowid = ?", (rowid,))
            deleted += 1
    t2.commit()
    print(f"committed: {updated} rows updated, {deleted} rows deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
