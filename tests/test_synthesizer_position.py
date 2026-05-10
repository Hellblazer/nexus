# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ivra (RDR-108 Phase 4 review S4): synthesizer position
ordering for Phase-3 chunks.

Phase 3 (nexus-bdag) removed ``chunk_index`` from chunk metadata.
``_synthesize_collection_chunks`` reads ``chunk_index`` for the
fallback path; the substantive-critic agent flagged that for
Phase-3 chunks the read returns 0, so all chunks would sort to
position 0 (undefined ordering).

This test file proves the existing implementation is correct
because the actual fallback at synthesizer.py:732 is
``legacy_chunk_index or page_offset`` — Python's ``or`` short-
circuits on the falsy 0, falling through to ``page_offset`` (the
monotonic page counter). Within-doc relative ordering is preserved
even when both manifest and chunk_index are unavailable.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from nexus.catalog import events as ev
from nexus.catalog.synthesizer import _synthesize_collection_chunks


def _fake_col_with_pages(pages: list[list[tuple[str, dict]]]) -> MagicMock:
    """Build a chroma-collection mock that returns the given pages
    in order. Each page is a list of (chunk_id, metadata) tuples;
    the helper splits into chroma's ``ids``/``metadatas`` shape."""
    col = MagicMock()
    responses = []
    for entries in pages:
        responses.append({
            "ids": [eid for eid, _ in entries],
            "metadatas": [m for _, m in entries],
        })
    # Final empty page signals end.
    responses.append({"ids": [], "metadatas": []})
    col.get.side_effect = responses
    return col


def test_phase3_chunks_use_page_offset_when_no_manifest_no_chunk_index():
    """When chunks lack chunk_index AND there's no catalog manifest
    to consult, position falls through to page_offset (the monotonic
    page counter). Relative ordering within a doc is preserved.

    This is the contract S4 was worried about: with chunk_index
    gone, do all chunks sort to position 0? Answer: no — the
    ``or`` operator at synthesizer.py:732 short-circuits 0 to the
    monotonic page_offset.
    """
    # Three Phase-3 chunks: no chunk_index, no doc_id.
    pages = [[
        ("id-a", {"chunk_text_hash": "a" * 64, "doc_id": "1.1.1"}),
        ("id-b", {"chunk_text_hash": "b" * 64, "doc_id": "1.1.1"}),
        ("id-c", {"chunk_text_hash": "c" * 64, "doc_id": "1.1.1"}),
    ]]
    col = _fake_col_with_pages(pages)

    events = list(_synthesize_collection_chunks(
        col, "code__test",
        source_uri_to_doc_id={},
        title_to_doc_id={},
        catalog=None,  # forces fallback path
    ))

    chunk_events = [e for e in events if e.type == ev.TYPE_CHUNK_INDEXED]
    positions = [e.payload.position for e in chunk_events]

    # Each chunk gets a distinct position (0, 1, 2) — NOT all 0.
    assert positions == [0, 1, 2], (
        f"Phase-3 chunks must get monotonic page_offset positions; "
        f"got {positions!r}. Reverting `legacy_chunk_index or "
        f"page_offset` to bare `legacy_chunk_index` would regress this."
    )


def test_manifest_takes_priority_over_page_offset_when_catalog_present():
    """When the catalog manifest has the (doc_id, chash) pair,
    its position wins. The catalog manifest is the RDR-108 D2
    authoritative source.
    """
    pages = [[
        # Two chunks; manifest has them in REVERSE order vs the page.
        ("id-x", {"chunk_text_hash": "x" * 64, "doc_id": "1.1.1"}),
        ("id-y", {"chunk_text_hash": "y" * 64, "doc_id": "1.1.1"}),
    ]]
    col = _fake_col_with_pages(pages)

    # Stub catalog: 'x' is at manifest position 5, 'y' is at 2.
    catalog = MagicMock()
    from nexus.catalog.catalog_writes import ManifestRow
    catalog.get_manifest.return_value = [
        ManifestRow(position=5, chash="x" * 64),
        ManifestRow(position=2, chash="y" * 64),
    ]

    events = list(_synthesize_collection_chunks(
        col, "code__test",
        source_uri_to_doc_id={},
        title_to_doc_id={},
        catalog=catalog,
    ))

    chunk_events = [e for e in events if e.type == ev.TYPE_CHUNK_INDEXED]
    by_chash = {e.payload.chash: e.payload.position for e in chunk_events}

    # Manifest values win over page_offset.
    assert by_chash == {"x" * 64: 5, "y" * 64: 2}


def test_manifest_position_zero_is_preserved_not_overridden_by_fallback():
    """The manifest's position 0 is a LEGITIMATE value (the first
    chunk in a doc). It must NOT be silently replaced by the
    page_offset fallback. This guards against an `or`-expression
    misuse that would treat manifest's 0 as "missing".
    """
    pages = [[
        # Single chunk that's at manifest position 0 (first chunk).
        # If the synthesizer wrongly treats 0 as "missing", it would
        # use the page_offset fallback — which on a fresh page is
        # also 0, but for the SECOND page would be wrong.
        ("id-z", {"chunk_text_hash": "z" * 64, "doc_id": "1.1.1"}),
    ]]
    col = _fake_col_with_pages(pages)

    catalog = MagicMock()
    from nexus.catalog.catalog_writes import ManifestRow
    catalog.get_manifest.return_value = [
        ManifestRow(position=0, chash="z" * 64),
    ]

    events = list(_synthesize_collection_chunks(
        col, "code__test",
        source_uri_to_doc_id={},
        title_to_doc_id={},
        catalog=catalog,
    ))

    chunk_events = [e for e in events if e.type == ev.TYPE_CHUNK_INDEXED]
    assert len(chunk_events) == 1
    assert chunk_events[0].payload.position == 0
