# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hjd6 (RDR-108 Phase 4 review D-H3): catalog_spans.resolve_span_text_for_entry's
chunk:char branch must use the catalog document_chunks manifest, not the
removed chunk_index/doc_id metadata fields.

Pre-fix the branch read where={chunk_index, doc_id} from T3 chunk
metadata. RDR-108 Phase 3 removed both fields; the where-filter
matched nothing for Phase-3 chunks and the function silently returned
None. Post-fix: the catalog manifest stores (doc_id, position, chash)
so we resolve position -> chash, then look up the chunk in T3 by its
content-addressed natural id (chash[:32]).
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database


@pytest.fixture()
def t3_db() -> T3Database:
    """T3Database backed by a fresh EphemeralClient. Mirrors the
    test_chash_reconcile.py / test_collection_gc.py pattern of
    clearing collections on entry to defend against the chromadb
    in-memory backend's session-shared state."""
    client = chromadb.EphemeralClient()
    for c in list(client.list_collections()):
        name = c if isinstance(c, str) else c.name
        try:
            client.delete_collection(name)
        except Exception:
            pass
    return T3Database(
        _client=client,
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    Catalog.init(catalog_dir)
    return Catalog(catalog_dir, catalog_dir / ".catalog.db")


def test_chunk_char_span_resolves_via_manifest(t3_db, catalog) -> None:
    """nexus-hjd6 contract: a chunk:char span on a Phase-3 chunk
    (no chunk_index/doc_id in metadata) must resolve to the correct
    text slice via the manifest. Reverting the manifest lookup makes
    this test fail (legacy where-filter returns nothing).
    """
    import hashlib as _hl

    # Seed: one Document with two chunks. Phase-3 metadata: only
    # chunk_text_hash (no chunk_index, no doc_id). The chunks are
    # written with chash[:32] as the chroma natural id (RDR-108 D1).
    coll_name = "docs__hjd6-test__voyage-context-3__v1"
    chunk_a_text = "alpha alpha alpha alpha alpha alpha"
    chunk_b_text = "beta beta beta beta beta beta beta beta"
    chash_a = _hl.sha256(chunk_a_text.encode()).hexdigest()
    chash_b = _hl.sha256(chunk_b_text.encode()).hexdigest()

    col = t3_db._client.get_or_create_collection(coll_name)
    col.upsert(
        ids=[chash_a[:32], chash_b[:32]],
        documents=[chunk_a_text, chunk_b_text],
        metadatas=[
            {"chunk_text_hash": chash_a},
            {"chunk_text_hash": chash_b},
        ],
    )

    # Register a Document and write its manifest. Position 0 -> chash_a;
    # position 1 -> chash_b. (Plain register_owner + register would
    # work but we want a deterministic tumbler for the test.)
    owner = catalog.register_owner("test-corpus", "corpus")
    tumbler = catalog.register(
        owner,
        title="hjd6.md",
        content_type="paper",
        file_path="hjd6.md",
        physical_collection=coll_name,
        chunk_count=2,
    )
    catalog.write_manifest(
        str(tumbler),
        [
            {"chash": chash_a, "position": 0, "line_start": 1, "line_end": 1,
             "char_start": 0, "char_end": len(chunk_a_text)},
            {"chash": chash_b, "position": 1, "line_start": 2, "line_end": 2,
             "char_start": 0, "char_end": len(chunk_b_text)},
        ],
    )

    # Patch make_t3 to return our test instance (the chunk:char branch
    # constructs T3 internally to read chroma).
    from unittest.mock import patch

    with patch("nexus.db.make_t3", return_value=t3_db):
        # chunk 0, chars 0-5 -> "alpha"
        text = catalog.resolve_span_text(tumbler, "0:0-5")
        assert text == "alpha", (
            f"chunk:char resolution must return slice; got {text!r}. "
            "Manifest lookup or chash[:32] T3 read regressed."
        )

        # chunk 1, chars 0-4 -> "beta"
        text2 = catalog.resolve_span_text(tumbler, "1:0-4")
        assert text2 == "beta", (
            f"second chunk:char resolution failed; got {text2!r}"
        )

        # chunk 1, full span (manifest reports char_end > 4) -> full text
        text3 = catalog.resolve_span_text(tumbler, "1:0-9999")
        assert text3 == chunk_b_text


def test_chunk_char_span_returns_none_when_position_out_of_range(
    t3_db, catalog,
) -> None:
    """A chunk:char span pointing at a position the manifest
    doesn't have must return None (not crash, not return wrong text).
    Defensive guard against operator-supplied span typos.
    """
    import hashlib as _hl
    from unittest.mock import patch

    coll_name = "docs__hjd6-oob__voyage-context-3__v1"
    chunk_text = "the only chunk"
    chash = _hl.sha256(chunk_text.encode()).hexdigest()
    col = t3_db._client.get_or_create_collection(coll_name)
    col.upsert(
        ids=[chash[:32]], documents=[chunk_text],
        metadatas=[{"chunk_text_hash": chash}],
    )

    owner = catalog.register_owner("oob-corpus", "corpus")
    tumbler = catalog.register(
        owner, title="oob.md", content_type="paper", file_path="oob.md",
        physical_collection=coll_name, chunk_count=1,
    )
    catalog.write_manifest(
        str(tumbler),
        [{"chash": chash, "position": 0, "char_start": 0,
          "char_end": len(chunk_text)}],
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        # Position 99 doesn't exist in the manifest.
        result = catalog.resolve_span_text(tumbler, "99:0-5")
        assert result is None, (
            f"out-of-range position must return None; got {result!r}"
        )
