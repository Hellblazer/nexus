#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed a LEGACY on-disk Chroma store — the pre-cutover state a real user has.

`make_t3()` returns the service client post-RDR-155-P4a (no local-write escape
hatch), so the only faithful way to produce the migration SOURCE is to write the
Chroma PersistentClient on disk directly, exactly as a pre-cutover install left
it. `nx migrate-to-service --local-path <here>` then detects + ETLs it.

Chunk shape mirrors the repo convention (tests/migration/test_vector_etl.py):
id = sha256(text)[:32] (the chash; round-trips verbatim into pgvector.chash),
documents = the text the service RE-EMBEDS (source vectors are never read by the
ETL), metadata = {position, tag}. Two conformant collections:

  knowledge__rehearsal__minilm-l6-v2-384__v1   (ONNX leg — re-embedded locally)
  knowledge__rehearsal__voyage-context-3__v1   (cloud leg — re-embedded via Voyage)

Usage: seed_legacy.py <chroma_path> [--with-cloud] [--n N]
Prints one JSON line: {"collections": {name: count, ...}} for the driver to assert.
"""
from __future__ import annotations

import hashlib
import json
import os

# Build the legacy T2 + catalog stores as raw SQLite, never the service backend
# — these ARE the migration source a pre-cutover (pre-5.10) nx left on disk.
# Set before importing nexus.db so storage_backend_for() resolves to SQLITE.
# Isolated to this process; the migrate command runs separately in service mode.
os.environ["NX_STORAGE_BACKEND"] = "sqlite"

import sys
from pathlib import Path

import chromadb

_MINILM = "knowledge__rehearsal__minilm-l6-v2-384__v1"
_VOYAGE = "knowledge__rehearsal__voyage-context-3__v1"
# RDR-162 P2: a SOURCELESS store_put-style note — a minilm-384 collection with
# NO backing source file (only a topic_assignment references it). embed_migrate
# (re-reads source files) cannot upgrade it; the cross-model migrate re-embeds
# its STORED text and re-points the assignment to the bge-768 target. This is the
# case that motivated RDR-162.
_NOTE = "knowledge__rehearsal-note__minilm-l6-v2-384__v1"

# The bge-768 targets the cross-model migrate re-embeds the minilm sources into
# (mirrors vector_etl.cross_model_target_name: only the model segment swaps).
_MINILM_TARGET = "knowledge__rehearsal__bge-base-en-v15-768__v1"
_NOTE_TARGET = "knowledge__rehearsal-note__bge-base-en-v15-768__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _seed(client, name: str, n: int, *, prefix: str) -> list[str]:
    """Seed a legacy Chroma collection; return the chunk chashes (ids)."""
    texts = [f"{prefix} {i:04d}" for i in range(n)]
    ids = [_chash(t) for t in texts]
    col = client.get_or_create_collection(name)
    col.add(
        ids=ids,
        documents=texts,
        metadatas=[{"position": i, "tag": "rehearsal"} for i in range(n)],
        # Source vectors are never read by the ETL (it re-embeds the documents
        # server-side); dim is deliberately nonsensical, matching the repo's
        # ETL fixtures.
        embeddings=[[float(i), 1.0] for i in range(n)],
    )
    return ids


def _seed_t2_and_catalog(collections: dict[str, list[str]]) -> dict[str, int]:
    """Build the legacy T2 memory.db (one note) + a catalog-CONSISTENT footprint.

    migrate-to-service sequences T2 → catalog → T3. The validation gate refuses
    to unlock when the migrated catalog is empty (orphan check would be vacuous —
    a false pass). So for each seeded Chroma collection we register a catalog
    document and write its document_chunks manifest referencing the SAME chashes,
    making the post-migration orphan scan (catalog manifest ⨝ pgvector chash)
    meaningful. Returns {"t2_notes": N, "catalog_docs": M}.
    """
    from nexus.config import nexus_config_dir
    from nexus.db.t2 import T2Database

    cfg = nexus_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    db = T2Database(cfg / "memory.db", run_migrations=True)
    db.memory.put(
        project="rehearsal", title="legacy-note",
        content="pre-cutover note", tags="rehearsal", ttl=0,
    )

    # RDR-162 P2: a SOURCELESS note assignment — a topic + a topic_assignment
    # whose ``source_collection`` is the note collection (_NOTE), with NO catalog
    # file document. The cross-model migrate must re-point this assignment to the
    # bge-768 target so the post-migration taxonomy-consistency check resolves.
    if _NOTE in collections:
        tax = db.taxonomy
        tax.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("rehearsal-note-topic", _NOTE, 1, "2026-06-18T00:00:00Z"),
        )
        topic_id = tax.conn.execute(
            "SELECT id FROM topics WHERE collection = ?", (_NOTE,)
        ).fetchone()[0]
        tax.conn.execute(
            "INSERT INTO topic_assignments "
            "(doc_id, topic_id, assigned_by, source_collection) "
            "VALUES (?, ?, 'manual', ?)",
            (collections[_NOTE][0], topic_id, _NOTE),
        )
        tax.conn.commit()

    from nexus.catalog.catalog import Catalog

    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat = Catalog.init(cat_dir) if not (cat_dir / ".catalog.db").exists() \
        else Catalog(cat_dir, cat_dir / ".catalog.db")

    repo_root = "/tmp/rehearsal-src"
    Path(repo_root).mkdir(parents=True, exist_ok=True)
    owner = cat.register_owner(
        "rehearsal", "project", repo_hash="rehearsal01", repo_root=repo_root,
    )
    docs = 0
    for coll, chashes in collections.items():
        # _NOTE is the SOURCELESS case: no catalog file document, only the
        # topic_assignment seeded above references it.
        if coll == _NOTE:
            continue
        fp = f"{repo_root}/{coll}.md"
        Path(fp).write_text("rehearsal legacy doc\n")
        doc = cat.register(
            owner, coll, content_type="knowledge", file_path=fp,
            physical_collection=coll, chunk_count=len(chashes),
        )
        cat.write_manifest(
            str(doc),
            [
                {"chash": c, "position": i, "line_start": None,
                 "line_end": None, "char_start": None, "char_end": None}
                for i, c in enumerate(chashes)
            ],
        )
        docs += 1
    return {"t2_notes": 1, "catalog_docs": docs}


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: seed_legacy.py <chroma_path> [--with-cloud] [--n N]", file=sys.stderr)
        return 2
    path = args[0]
    with_cloud = "--with-cloud" in args
    n = 12
    if "--n" in args:
        n = int(args[args.index("--n") + 1])

    client = chromadb.PersistentClient(path=path)
    chashes: dict[str, list[str]] = {}
    chashes[_MINILM] = _seed(client, _MINILM, n, prefix="onnx chunk")
    chashes[_NOTE] = _seed(client, _NOTE, n, prefix="note chunk")
    if with_cloud:
        chashes[_VOYAGE] = _seed(client, _VOYAGE, n, prefix="voyage chunk")
    t2 = _seed_t2_and_catalog(chashes)
    seeded = {name: len(ids) for name, ids in chashes.items()}
    # cross_model: source -> bge-768 target the migrate re-embeds into. The
    # voyage leg (when present) migrates byte-for-byte (servable with a key), so
    # it is NOT remapped and is absent from this map.
    cross_model = {_MINILM: _MINILM_TARGET, _NOTE: _NOTE_TARGET}
    print(json.dumps({"collections": seeded, "cross_model": cross_model, **t2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
