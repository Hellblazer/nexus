#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-olg5: split docs__ART-8c2e74c0 by moving the 78 PDF papers
under /docs/papers/ to a new ``knowledge__art-papers`` collection.

Metadata-only migration: chunks keep their existing embeddings and
documents (so no Voyage cost). Per-chunk metadata is rewritten so
each chunk's ``title`` carries a paper-shaped name derived from the
filename, ``category`` flips from "prose" to "paper", and
``content_type`` stays "pdf".

Three layers move together:

1. **T3 chunks** — read from docs__ART, add to knowledge__art-papers
   with same ids + embeddings + documents but rewritten metadata,
   delete from docs__ART.
2. **T2 aspects** — ``UPDATE document_aspects SET collection =
   'knowledge__art-papers' WHERE source_path LIKE '%/docs/papers/%'``.
3. **Catalog** — ``UPDATE documents SET physical_collection =
   'knowledge__art-papers' WHERE physical_collection =
   'docs__ART-8c2e74c0' AND file_path LIKE 'docs/papers/%'``.

Run with ``--dry-run`` first to see what would change. Without
``--dry-run`` the script asks for confirmation before writing.

Reversible: the inverse migration would copy the chunks back, restore
the old metadata (lost — backups not made), and reverse the SQL. In
practice the reverse path is just "re-index the PDFs from disk", so
we accept the metadata loss.
"""
from __future__ import annotations

import argparse
import sys
import sqlite3
from pathlib import Path
from typing import Any

# nx package import
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.db import make_t3
from nexus.indexer_utils import derive_title
from nexus.commands._helpers import default_db_path
from nexus.config import catalog_path

FROM_COLLECTION = "docs__ART-8c2e74c0"
TO_COLLECTION = "knowledge__art-papers"
PATH_PREFIX = "/Users/hal.hildebrand/git/ART/docs/papers/"
T2_PATH_LIKE = "docs/papers/%"
CATALOG_PATH_PREFIX = "docs/papers/"

PAGE = 300  # ChromaDB Cloud read/write cap


def _rewrite_metadata(meta: dict, source_path: str) -> dict:
    """Rewrite chunk metadata for a paper-shaped collection.

    Replaces the ``X.pdf:page-N`` placeholder title with a derived
    title from the filename; flips category to "paper". Other fields
    pass through unchanged so we don't lose embedding_model,
    chunk_index, content_hash, git_meta, etc.
    """
    new = dict(meta)
    pdf_path = Path(source_path)
    new["title"] = derive_title(pdf_path, body=None)
    new["category"] = "paper"
    return new


def _scan_pdf_chunks(col, prefix: str) -> tuple[list[str], list[Any], list[str], list[dict]]:
    """Page through ``col`` and collect chunks whose source_path starts
    with ``prefix``. Returns parallel lists.
    """
    ids: list[str] = []
    embeds: list[Any] = []
    docs: list[str] = []
    metas: list[dict] = []

    offset = 0
    while True:
        page = col.get(
            limit=PAGE, offset=offset,
            include=["embeddings", "documents", "metadatas"],
        )
        page_ids = page.get("ids") or []
        if not page_ids:
            break
        # ChromaDB returns embeddings as numpy arrays; ``or`` triggers
        # the ambiguous-truth check. Use explicit None comparison.
        page_embeds = page.get("embeddings")
        if page_embeds is None:
            page_embeds = []
        page_docs = page.get("documents") or []
        page_metas = page.get("metadatas") or []
        for cid, emb, doc, meta in zip(page_ids, page_embeds, page_docs, page_metas):
            sp = (meta or {}).get("source_path", "")
            if sp.startswith(prefix):
                ids.append(cid)
                embeds.append(emb)
                docs.append(doc)
                metas.append(meta)
        if len(page_ids) < PAGE:
            break
        offset += PAGE

    return ids, embeds, docs, metas


def _add_in_batches(col, ids, embeds, docs, metas, batch: int = 200) -> None:
    """ChromaDB Cloud caps writes at 300 records; use 200 for headroom."""
    for i in range(0, len(ids), batch):
        end = i + batch
        col.add(
            ids=ids[i:end],
            embeddings=embeds[i:end],
            documents=docs[i:end],
            metadatas=metas[i:end],
        )


def _delete_in_batches(col, ids, batch: int = 200) -> None:
    for i in range(0, len(ids), batch):
        col.delete(ids=ids[i:i + batch])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    args = parser.parse_args()

    db = make_t3()
    src = db.get_or_create_collection(FROM_COLLECTION)
    dst = db.get_or_create_collection(TO_COLLECTION)

    print(f"Scanning {FROM_COLLECTION} for chunks under {PATH_PREFIX}...")
    ids, embeds, docs, metas = _scan_pdf_chunks(src, PATH_PREFIX)
    print(f"  Found {len(ids)} chunks across "
          f"{len({(m or {}).get('source_path', '') for m in metas})} unique PDFs.")

    if not ids:
        print("Nothing to move. Exiting.")
        return 0

    print(f"\nRewriting metadata (new titles via derive_title)...")
    rewritten_metas = [
        _rewrite_metadata(m, (m or {}).get("source_path", ""))
        for m in metas
    ]
    sample = sorted({m["title"] for m in rewritten_metas})[:5]
    print(f"  Sample new titles:")
    for t in sample:
        print(f"    {t!r}")

    # Plan T2 + catalog updates.
    db_path = default_db_path()
    cat_path = catalog_path()
    cat_db = cat_path / ".catalog.db"

    aspect_count = 0
    catalog_count = 0
    with sqlite3.connect(db_path) as t2_conn:
        cur = t2_conn.execute(
            "SELECT COUNT(*) FROM document_aspects "
            "WHERE collection = ? AND source_path LIKE ?",
            (FROM_COLLECTION, T2_PATH_LIKE),
        )
        aspect_count = cur.fetchone()[0]
    with sqlite3.connect(cat_db) as cat_conn:
        cur = cat_conn.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE physical_collection = ? AND file_path LIKE ?",
            (FROM_COLLECTION, CATALOG_PATH_PREFIX + "%"),
        )
        catalog_count = cur.fetchone()[0]

    print(f"\nPlan:")
    print(f"  T3: add {len(ids)} chunks to {TO_COLLECTION}, delete same ids from {FROM_COLLECTION}.")
    print(f"  T2: UPDATE {aspect_count} document_aspects rows to collection={TO_COLLECTION!r}.")
    print(f"  Catalog: UPDATE {catalog_count} documents rows to physical_collection={TO_COLLECTION!r}.")

    if args.dry_run:
        print("\n[dry-run] No writes performed.")
        return 0

    if not args.yes:
        confirm = input("\nProceed? (yes/no) ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return 1

    print(f"\nWriting {len(ids)} chunks to {TO_COLLECTION}...")
    _add_in_batches(dst, ids, embeds, docs, rewritten_metas)
    print(f"  Done.")

    print(f"Deleting {len(ids)} chunks from {FROM_COLLECTION}...")
    _delete_in_batches(src, ids)
    print(f"  Done.")

    print(f"Updating T2 document_aspects ({aspect_count} rows)...")
    with sqlite3.connect(db_path) as t2_conn:
        t2_conn.execute(
            "UPDATE document_aspects SET collection = ? "
            "WHERE collection = ? AND source_path LIKE ?",
            (TO_COLLECTION, FROM_COLLECTION, T2_PATH_LIKE),
        )
        t2_conn.commit()
    print(f"  Done.")

    print(f"Updating catalog documents ({catalog_count} rows)...")
    # nexus-olg5 fix: catalog uses JSONL-as-truth + SQLite-as-cache.
    # A direct SQL UPDATE writes to the cache only; the next
    # consistency rebuild from JSONL silently reverts it. Use
    # cat.update() so the change appends to JSONL too.
    from nexus.catalog import Catalog as _Cat
    from nexus.catalog.tumbler import Tumbler as _Tum
    cat = _Cat(cat_path, cat_db)
    rows = cat._db.execute(
        "SELECT tumbler FROM documents "
        "WHERE physical_collection = ? AND file_path LIKE ?",
        (FROM_COLLECTION, CATALOG_PATH_PREFIX + "%"),
    ).fetchall()
    for (t_str,) in rows:
        cat.update(_Tum.parse(t_str), physical_collection=TO_COLLECTION)
    print(f"  Done.")

    print(f"\nMigration complete. Verify:")
    print(f"  nx collection list | grep -E 'docs__ART|knowledge__art-papers'")
    print(f"  nx catalog audit-membership {TO_COLLECTION}")
    print(f"  nx enrich bib {TO_COLLECTION}  # OpenAlex polite pool, no key needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
