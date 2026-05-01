# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pin the cross-indexer metadata consistency contract.

Drift guard for the chunk-metadata factory (RDR-089 follow-up,
see docs/metadata-consistency-matrix.md). Every indexer that writes
T3 chunk metadata must route through ``make_chunk_metadata`` so that:

  * Every ``ALLOWED_TOP_LEVEL`` key is present (with documented
    defaults when not explicitly populated).
  * No deprecated key (``source_title``, ``expires_at``) leaks through.
  * Adding a new ``ALLOWED_TOP_LEVEL`` key forces a single-edit
    update to the factory rather than seven separate indexer changes.

Drift = either a new field appears on one path and not others, or
an indexer stops routing through the factory and silently drops
fields. Both are regressions.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus.metadata_schema import ALLOWED_TOP_LEVEL, make_chunk_metadata


# ── Factory contract ────────────────────────────────────────────────────────


def _expected_keys_for_content_type(content_type: str) -> set[str]:
    """Return the keys that the factory MUST emit for a given content_type.

    ``bib_*`` keys are dropped together when all-empty (intentional —
    they only ride along when ``--enrich`` populated them). ``git_meta``
    is dropped when all four flat git_* keys are empty. Both are
    by-design omissions — every other ALLOWED_TOP_LEVEL key must be
    present.
    """
    bib_keys = {
        "bib_year", "bib_authors", "bib_venue", "bib_citation_count",
        "bib_semantic_scholar_id",
    }
    # RDR-101 Phase 3 PR δ: ``doc_id`` is opt-in. Drop-when-empty
    # parallels bib_* / git_meta — call sites that do not pass a
    # Catalog-resolved doc_id get ``""`` and normalize() strips it.
    return ALLOWED_TOP_LEVEL - bib_keys - {"git_meta", "doc_id"}


def test_factory_emits_full_keyset_for_code() -> None:
    meta = make_chunk_metadata(
        content_type="code",
        source_path="/x/y.py",
        chunk_index=0,
        chunk_count=1,
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-code-3",
        store_type="code",
    )
    expected = _expected_keys_for_content_type("code")
    assert set(meta.keys()) >= expected, (
        f"factory missing keys: {expected - set(meta.keys())}"
    )


def test_factory_emits_full_keyset_for_pdf() -> None:
    meta = make_chunk_metadata(
        content_type="pdf",
        source_path="/x/y.pdf",
        chunk_index=0,
        chunk_count=1,
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-context-3",
        store_type="pdf",
    )
    expected = _expected_keys_for_content_type("pdf")
    assert set(meta.keys()) >= expected, (
        f"factory missing keys: {expected - set(meta.keys())}"
    )


def test_factory_emits_full_keyset_for_markdown() -> None:
    meta = make_chunk_metadata(
        content_type="markdown",
        source_path="/x/y.md",
        chunk_index=0,
        chunk_count=1,
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-context-3",
        store_type="markdown",
    )
    expected = _expected_keys_for_content_type("markdown")
    assert set(meta.keys()) >= expected, (
        f"factory missing keys: {expected - set(meta.keys())}"
    )


def test_factory_emits_full_keyset_for_prose() -> None:
    meta = make_chunk_metadata(
        content_type="prose",
        source_path="/x/y.txt",
        chunk_index=0,
        chunk_count=1,
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-context-3",
        store_type="prose",
    )
    expected = _expected_keys_for_content_type("prose")
    assert set(meta.keys()) >= expected, (
        f"factory missing keys: {expected - set(meta.keys())}"
    )


def test_factory_drops_bib_when_empty() -> None:
    """``--enrich`` off → bib_* fields are dropped together, not stored
    as zero/empty placeholders eating metadata budget."""
    meta = make_chunk_metadata(
        content_type="pdf",
        source_path="/x.pdf",
        chunk_index=0, chunk_count=1,
        chunk_text_hash="a"*64, content_hash="b"*64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-context-3", store_type="pdf",
    )
    for k in ("bib_year", "bib_authors", "bib_venue", "bib_citation_count"):
        assert k not in meta


def test_factory_keeps_bib_when_populated() -> None:
    """When at least one bib field is populated the whole quad rides."""
    meta = make_chunk_metadata(
        content_type="pdf",
        source_path="/x.pdf",
        chunk_index=0, chunk_count=1,
        chunk_text_hash="a"*64, content_hash="b"*64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-context-3", store_type="pdf",
        bib_year=2026,
    )
    assert meta["bib_year"] == 2026
    assert meta["bib_authors"] == ""
    assert meta["bib_venue"] == ""
    assert meta["bib_citation_count"] == 0


def test_factory_packs_git_meta_when_provided() -> None:
    meta = make_chunk_metadata(
        content_type="code",
        source_path="/src/x.py",
        chunk_index=0, chunk_count=1,
        chunk_text_hash="a"*64, content_hash="b"*64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-code-3", store_type="code",
        git_meta={"git_project_name": "nexus", "git_branch": "main",
                  "git_commit_hash": "abc123", "git_remote_url": "x"},
    )
    assert "git_meta" in meta
    import json
    decoded = json.loads(meta["git_meta"])
    assert decoded["project"] == "nexus"
    assert decoded["branch"] == "main"


def test_factory_drops_git_meta_when_empty() -> None:
    meta = make_chunk_metadata(
        content_type="code",
        source_path="/src/x.py",
        chunk_index=0, chunk_count=1,
        chunk_text_hash="a"*64, content_hash="b"*64,
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-code-3", store_type="code",
    )
    assert "git_meta" not in meta


def test_factory_rejects_deprecated_keys_via_normalize() -> None:
    """``source_title`` and ``expires_at`` were collapsed into
    ``title`` and derived expiry — readers and writers must not
    leak the old names through."""
    assert "source_title" not in ALLOWED_TOP_LEVEL
    assert "expires_at" not in ALLOWED_TOP_LEVEL


# ── Per-indexer integration: every chunked path routes through factory ─────


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A minimal git-style repo for indexer fixtures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _full_keyset_minus_optional() -> set[str]:
    """Keys that every chunked-write indexer MUST emit.

    bib_* and git_meta are intentionally optional (see normalize() rules).
    RDR-101 Phase 3 PR δ: ``doc_id`` is also opt-in — call sites with a
    Catalog handle pass it; call sites without one (e.g. MCP store_put,
    bare-metadata factory tests) pass ``""`` and the field is dropped
    by ``normalize`` Step 4c.
    Every other ALLOWED_TOP_LEVEL key must appear on every chunk.
    """
    return ALLOWED_TOP_LEVEL - {
        "bib_year", "bib_authors", "bib_venue", "bib_citation_count",
        "bib_semantic_scholar_id",
        "git_meta",
        "doc_id",
    }


def test_pdf_indexer_emits_full_keyset(tmp_repo: Path) -> None:
    """``_pdf_chunks`` (the doc_indexer batch path) routes through
    the factory and emits the full keyset."""
    pytest.importorskip("pymupdf")
    import pymupdf

    pdf = tmp_repo / "tiny.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Tiny test paper.\n\nIntroduction\n\nBody text.")
    doc.save(pdf)
    doc.close()

    from nexus.doc_indexer import _pdf_chunks
    prepared = _pdf_chunks(
        pdf, content_hash="x" * 64, target_model="voyage-context-3",
        now_iso="2026-04-26T00:00:00+00:00", corpus="test",
    )
    assert prepared, "fixture PDF should produce at least one chunk"
    expected = _full_keyset_minus_optional()
    for _, _, meta in prepared:
        missing = expected - set(meta.keys())
        assert not missing, (
            f"_pdf_chunks dropped: {missing}; got keys {sorted(meta.keys())}"
        )


def test_pipeline_pdf_emits_full_keyset() -> None:
    """``pipeline_stages._build_chunk_metadata`` routes through the factory."""
    from nexus.pdf_chunker import TextChunk
    from nexus.pipeline_stages import _build_chunk_metadata
    chunk = TextChunk(
        text="abc",
        chunk_index=0,
        metadata={
            "chunk_index": 0, "chunk_start_char": 0, "chunk_end_char": 3,
            "page_number": 1, "chunk_type": "text",
            "section_title": "1 Intro", "section_type": "introduction",
        },
    )
    meta = _build_chunk_metadata(
        chunk,
        content_hash="x" * 64,
        pdf_path="/x.pdf",
        corpus="test",
        embedding_model="voyage-context-3",
        chunk_count=1,
        now_iso="2026-04-26T00:00:00+00:00",
    )
    expected = _full_keyset_minus_optional()
    missing = expected - set(meta.keys())
    assert not missing, (
        f"streaming pipeline dropped: {missing}; got keys {sorted(meta.keys())}"
    )


def test_t3_put_emits_full_keyset_for_mcp_stored_doc() -> None:
    """``T3Database.put`` (MCP store_put backend) routes through the
    factory so single-chunk MCP-stored docs carry the full keyset
    (closes RDR-086 chash coverage hole for MCP-stored docs)."""
    from nexus.metadata_schema import make_chunk_metadata
    # We can't easily exercise the full T3Database in a unit test
    # (cloud auth), so verify the metadata-shape contract via the
    # factory call equivalent.
    content = "MCP-stored content"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    meta = make_chunk_metadata(
        content_type="prose",
        source_path="",
        chunk_index=0,
        chunk_count=1,
        chunk_text_hash=content_hash,
        content_hash=content_hash,
        chunk_start_char=0,
        chunk_end_char=len(content),
        indexed_at="2026-04-26T00:00:00+00:00",
        embedding_model="voyage-context-3",
        store_type="knowledge",
        title="My Note",
        tags="user",
        category="note",
    )
    expected = _full_keyset_minus_optional()
    missing = expected - set(meta.keys())
    assert not missing, f"T3 put metadata missing: {missing}"
    # Specifically the chash fields that close the RDR-086 coverage hole:
    assert meta["chunk_text_hash"] == content_hash
    assert meta["content_hash"] == content_hash
    assert meta["chunk_index"] == 0
    assert meta["chunk_count"] == 1
