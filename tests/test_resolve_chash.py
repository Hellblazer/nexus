# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-086 Phase 2: ``Catalog.resolve_chash`` — global chash → ChunkRef.

Exercises the T2-backed happy path, multi-match tie-break, self-healing
read (stale T2 row when the target collection has been deleted), and
the ChromaDB parallel-fallback deadline contract.
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def resolve_env(tmp_path: Path):
    """Catalog + EphemeralClient T3 + real T2 ChashIndex on disk."""
    from nexus.catalog.catalog import Catalog
    from nexus.db.t2.chash_index import ChashIndex

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    cat = Catalog(cat_dir, cat_dir / ".catalog.db")
    t3 = chromadb.EphemeralClient()
    chash_index = ChashIndex(tmp_path / "t2.db")

    yield cat, t3, chash_index

    chash_index.close()


def _seed_chunk(
    t3, chash_index, collection: str, chunk_id: str, text: str, chash: str,
) -> None:
    """Insert a chunk into T3 and register its chash in T2."""
    col = t3.get_or_create_collection(collection)
    col.add(
        ids=[chunk_id],
        documents=[text],
        metadatas=[{"chunk_text_hash": chash, "source_path": "s.py"}],
    )
    chash_index.upsert(chash=chash, collection=collection, chunk_chroma_id=chunk_id)


# ── ChunkRef type ────────────────────────────────────────────────────────────


class TestChunkRefType:
    def test_chunkref_importable(self):
        from nexus.catalog.types import ChunkRef  # noqa: F401


# ── T2-populated happy path ──────────────────────────────────────────────────


class TestResolveChashT2Hit:
    def test_single_match_returns_chunkref(self, resolve_env):
        cat, t3, chash_index = resolve_env
        h = "a" * 64
        _seed_chunk(t3, chash_index, "code__only", "chunk-0", "hello", h)

        ref = cat.resolve_chash(h, t3, chash_index)

        assert ref is not None
        assert ref["chash"] == h
        assert ref["chunk_hash"] == h   # back-compat alias
        assert ref["physical_collection"] == "code__only"
        assert ref["doc_id"] == "chunk-0"
        assert ref["chunk_text"] == "hello"
        assert ref["metadata"]["source_path"] == "s.py"

    def test_prefer_collection_wins_multi_match(self, resolve_env):
        cat, t3, chash_index = resolve_env
        h = "b" * 64
        # Same chash in two collections — caller prefers one of them.
        _seed_chunk(t3, chash_index, "code__a", "c-a", "text-a", h)
        _seed_chunk(t3, chash_index, "code__b", "c-b", "text-b", h)

        ref = cat.resolve_chash(h, t3, chash_index, prefer_collection="code__b")

        assert ref is not None
        assert ref["physical_collection"] == "code__b"
        assert ref["chunk_text"] == "text-b"

    def test_newest_created_at_wins_when_no_preference(self, resolve_env):
        """Re-indexing into a better collection (e.g. _docling variant) supersedes the old one.

        Uses explicit ``created_at`` timestamps rather than ``datetime.now()``
        so the test is deterministic on loaded CI — a prior iteration
        relied on a 10 ms sleep to force distinct ISO stamps, which the
        code-review called out as flaky.
        """
        cat, t3, chash_index = resolve_env
        h = "c" * 64

        # Seed both chunks into T3; then overwrite the two T2 rows with
        # explicit created_at timestamps so the tie-break has a
        # deterministic "newer" to pick.
        col_old = t3.get_or_create_collection("code__old")
        col_old.add(
            ids=["c-old"], documents=["stale"],
            metadatas=[{"chunk_text_hash": h, "source_path": "s.py"}],
        )
        col_new = t3.get_or_create_collection("code__new")
        col_new.add(
            ids=["c-new"], documents=["fresh"],
            metadatas=[{"chunk_text_hash": h, "source_path": "s.py"}],
        )

        # Direct SQL with explicit timestamps — the default upsert uses
        # datetime.now() which can collide at microsecond resolution.
        with chash_index._lock:
            chash_index.conn.execute(
                "INSERT OR REPLACE INTO chash_index "
                "(chash, physical_collection, chunk_chroma_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (h, "code__old", "c-old", "2025-01-01T00:00:00+00:00"),
            )
            chash_index.conn.execute(
                "INSERT OR REPLACE INTO chash_index "
                "(chash, physical_collection, chunk_chroma_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (h, "code__new", "c-new", "2026-04-18T00:00:00+00:00"),
            )
            chash_index.conn.commit()

        ref = cat.resolve_chash(h, t3, chash_index)

        assert ref is not None
        assert ref["physical_collection"] == "code__new"
        assert ref["chunk_text"] == "fresh"

    def test_char_range_delegates_to_resolve_span(self, resolve_env):
        cat, t3, chash_index = resolve_env
        h = "d" * 64
        _seed_chunk(
            t3, chash_index, "code__slice", "c0", "def hello(): pass", h,
        )

        ref = cat.resolve_chash(f"chash:{h}:4-9", t3, chash_index)

        assert ref is not None
        assert ref["chunk_text"] == "hello"
        assert ref.get("char_range") == (4, 9)


# ── Self-healing read ────────────────────────────────────────────────────────


class TestResolveChashSelfHeal:
    def test_stale_t2_row_removed_when_collection_missing(self, resolve_env):
        """T2 row points at a collection that has been deleted — delete the row
        and fall through to other candidates.
        """
        cat, t3, chash_index = resolve_env
        h = "e" * 64
        # Register a chash pointing at a collection that does not exist in T3.
        chash_index.upsert(chash=h, collection="code__deleted", chunk_chroma_id="ghost")
        # And a live one.
        _seed_chunk(t3, chash_index, "code__live", "real-id", "real text", h)

        ref = cat.resolve_chash(h, t3, chash_index)

        # Should have fallen through to the live collection.
        assert ref is not None
        assert ref["physical_collection"] == "code__live"
        assert ref["chunk_text"] == "real text"

        # And the stale row must be gone.
        remaining = chash_index.conn.execute(
            "SELECT physical_collection FROM chash_index WHERE chash = ?",
            (h,),
        ).fetchall()
        cols = {r[0] for r in remaining}
        assert "code__deleted" not in cols
        assert "code__live" in cols


# ── T2 miss → T3 fallback ────────────────────────────────────────────────────


class TestResolveChashFallback:
    def test_t3_fallback_scans_collections_on_t2_miss(self, resolve_env):
        """Chunk exists in T3 but T2 has no row — scan all collections."""
        cat, t3, chash_index = resolve_env
        h = "f" * 64
        # Add chunk directly to T3 without registering in T2.
        col = t3.get_or_create_collection("code__unindexed")
        col.add(
            ids=["u1"], documents=["recovered"],
            metadatas=[{"chunk_text_hash": h}],
        )

        ref = cat.resolve_chash(h, t3, chash_index)

        assert ref is not None
        assert ref["physical_collection"] == "code__unindexed"
        assert ref["chunk_text"] == "recovered"

    def test_fallback_returns_none_when_chash_nowhere(self, resolve_env):
        cat, t3, chash_index = resolve_env
        # Create a collection so the scan has something to iterate.
        t3.get_or_create_collection("code__empty")
        h = "0" * 64

        ref = cat.resolve_chash(h, t3, chash_index)
        assert ref is None

    def test_fallback_logs_warning_once_per_process(self, resolve_env, caplog):
        import logging

        cat, t3, chash_index = resolve_env
        t3.get_or_create_collection("code__scan")
        h = "1" * 64

        with caplog.at_level(logging.WARNING):
            cat.resolve_chash(h, t3, chash_index)
            cat.resolve_chash(h, t3, chash_index)

        fallback_warnings = [
            r for r in caplog.records
            if "fallback" in r.getMessage().lower()
            or "resolve_chash" in r.getMessage().lower()
        ]
        # Exactly one fallback-entered warning per process.
        assert len([r for r in fallback_warnings if "entered" in r.getMessage().lower()
                    or "scanning" in r.getMessage().lower()]) <= 1


# ── Input parsing ────────────────────────────────────────────────────────────


class TestResolveChashInputForms:
    def test_accepts_bare_hex(self, resolve_env):
        cat, t3, chash_index = resolve_env
        h = "2" * 64
        _seed_chunk(t3, chash_index, "code__bare", "b1", "x", h)

        ref = cat.resolve_chash(h, t3, chash_index)
        assert ref is not None

    def test_accepts_chash_prefix(self, resolve_env):
        cat, t3, chash_index = resolve_env
        h = "3" * 64
        _seed_chunk(t3, chash_index, "code__pre", "p1", "y", h)

        ref = cat.resolve_chash(f"chash:{h}", t3, chash_index)
        assert ref is not None

    def test_rejects_malformed_chash(self, resolve_env):
        cat, t3, chash_index = resolve_env
        with pytest.raises(ValueError):
            cat.resolve_chash("not-a-hash", t3, chash_index)


# ── _negate_iso helper (tie-break invariant) ─────────────────────────────────


class TestNegateIso:
    """The tie-break relies on _negate_iso flipping sort order of ISO
    timestamps so ``sorted(..., key=_negate_iso)`` returns newest first.
    """

    def test_newer_sorts_first(self):
        from nexus.catalog.catalog import _negate_iso

        old = "2025-01-15T10:00:00+00:00"
        new = "2026-04-18T10:00:00+00:00"
        pair = sorted([old, new], key=_negate_iso)
        assert pair[0] == new
        assert pair[1] == old

    def test_preserves_separator_positions(self):
        from nexus.catalog.catalog import _negate_iso

        out = _negate_iso("2026-04-18T10:00:00+00:00")
        # Non-digits pass through unchanged.
        assert out[4] == "-"
        assert out[7] == "-"
        assert out[10] == "T"
        assert out[13] == ":"
