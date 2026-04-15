# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for src/nexus/metadata_schema.py (nexus-40t).

Covers normalisation, schema validation, and the round-trip of the
consolidated ``git_meta`` JSON blob.
"""
from __future__ import annotations

import json

import pytest


# ── Allowed key set ─────────────────────────────────────────────────────────


def test_allowed_top_level_is_bounded() -> None:
    """The canonical schema fits inside MAX_SAFE_TOP_LEVEL_KEYS (≤28)."""
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL, MAX_SAFE_TOP_LEVEL_KEYS

    assert len(ALLOWED_TOP_LEVEL) <= MAX_SAFE_TOP_LEVEL_KEYS
    assert MAX_SAFE_TOP_LEVEL_KEYS < 32, "must stay below Chroma 32-key cap"


def test_load_bearing_keys_are_allowed() -> None:
    """Every key read by ``where=`` filters or scoring must be top-level."""
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL

    for key in (
        "source_path",
        "content_hash",
        "chunk_text_hash",
        "chunk_index",
        "chunk_count",
        "chunk_start_char",
        "chunk_end_char",
        "page_number",
        "bib_year",
        "ttl_days",
        "expires_at",
        "section_type",
        "frecency_score",
        "source_agent",
    ):
        assert key in ALLOWED_TOP_LEVEL, f"load-bearing key {key!r} missing"


def test_content_type_is_canonical() -> None:
    from nexus.metadata_schema import CONTENT_TYPES

    assert CONTENT_TYPES == frozenset({"code", "pdf", "markdown", "prose"})


def test_cargo_keys_not_allowed() -> None:
    """Keys confirmed never-read must not leak into top-level schema."""
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL

    for key in (
        "bib_semantic_scholar_id",
        "pdf_subject",
        "pdf_keywords",
        "source_date",
        "format",
        "extraction_method",
        "chunk_type",
        "filename",
        "file_extension",
        "programming_language",
        "ast_chunked",
        "page_count",
        "indexed_at",
        "is_image_pdf",
        "has_formulas",
        "git_project_name",
        "git_branch",
        "git_commit_hash",
        "git_remote_url",
    ):
        assert key not in ALLOWED_TOP_LEVEL, f"cargo key {key!r} should be dropped"


# ── normalize() ─────────────────────────────────────────────────────────────


def test_normalize_drops_unknown_keys() -> None:
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "/a.pdf",
        "content_hash": "abc",
        "chunk_text_hash": "def",
        "chunk_index": 0,
        "chunk_count": 1,
        "pdf_subject": "should drop",
        "extraction_method": "should drop",
        "ast_chunked": True,
    }
    out = normalize(raw, content_type="pdf")
    assert "pdf_subject" not in out
    assert "extraction_method" not in out
    assert "ast_chunked" not in out
    assert out["source_path"] == "/a.pdf"


def test_normalize_consolidates_git_meta() -> None:
    """git_* keys collapse into a single JSON string under ``git_meta``."""
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "src/foo.py",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "git_project_name": "nexus",
        "git_branch": "main",
        "git_commit_hash": "deadbeef",
        "git_remote_url": "https://github.com/example/nexus.git",
    }
    out = normalize(raw, content_type="code")
    assert "git_project_name" not in out
    assert "git_branch" not in out
    assert "git_meta" in out
    decoded = json.loads(out["git_meta"])
    assert decoded == {
        "project": "nexus",
        "branch": "main",
        "commit": "deadbeef",
        "remote": "https://github.com/example/nexus.git",
    }


def test_normalize_omits_git_meta_when_all_empty() -> None:
    """No git data → no ``git_meta`` key (headroom conservation)."""
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "a.md",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "git_project_name": "",
        "git_branch": "",
        "git_commit_hash": "",
        "git_remote_url": "",
    }
    out = normalize(raw, content_type="markdown")
    assert "git_meta" not in out


def test_normalize_injects_content_type() -> None:
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "f.py",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
    }
    out = normalize(raw, content_type="code")
    assert out["content_type"] == "code"


def test_normalize_rejects_invalid_content_type() -> None:
    from nexus.metadata_schema import normalize

    with pytest.raises(ValueError, match="content_type"):
        normalize({"source_path": "a", "content_hash": "x",
                   "chunk_text_hash": "y", "chunk_index": 0, "chunk_count": 1},
                  content_type="binary")


def test_normalize_preserves_bib_fields() -> None:
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "p.pdf",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "bib_year": 2024,
        "bib_authors": "Smith, Jones",
        "bib_venue": "ICML",
        "bib_citation_count": 42,
        "bib_semantic_scholar_id": "dropthis",
    }
    out = normalize(raw, content_type="pdf")
    assert out["bib_year"] == 2024
    assert out["bib_authors"] == "Smith, Jones"
    assert out["bib_venue"] == "ICML"
    assert out["bib_citation_count"] == 42
    assert "bib_semantic_scholar_id" not in out


def test_normalize_is_idempotent() -> None:
    """Re-normalising a normalised dict produces byte-identical output.

    This is load-bearing: the enrichment post-pass reads the existing
    metadata and re-normalises with updates. It must not accrete keys.
    """
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "src/foo.py",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "git_project_name": "nexus",
        "git_branch": "main",
        "git_commit_hash": "deadbeef",
        "git_remote_url": "https://example.com",
    }
    first = normalize(raw, content_type="code")
    second = normalize(first, content_type="code")
    assert first == second


def test_normalize_unpacks_git_meta_on_reprocess() -> None:
    """Re-normalising unpacks ``git_meta`` back so downstream updates
    can merge with git fields without losing data."""
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "src/foo.py",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "git_project_name": "nexus",
        "git_branch": "main",
        "git_commit_hash": "abc",
        "git_remote_url": "https://example.com",
    }
    first = normalize(raw, content_type="code")

    # Simulate a post-pass that merges new fields with the existing row.
    merged = {**first, "bib_year": 2024, "bib_authors": "Doe"}
    second = normalize(merged, content_type="code")
    decoded = json.loads(second["git_meta"])
    assert decoded["commit"] == "abc"
    assert second["bib_year"] == 2024


def test_normalize_drops_empty_ttl_defaults() -> None:
    """``ttl_days=0, expires_at=''`` are load-bearing (permanent signal) and kept."""
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "a",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "ttl_days": 0,
        "expires_at": "",
    }
    out = normalize(raw, content_type="code")
    assert out["ttl_days"] == 0
    assert out["expires_at"] == ""


def test_normalize_drops_empty_bib_placeholders() -> None:
    """Bib_* slots with placeholder values (``0`` / ``""``) eat metadata
    budget for no payload when ``--enrich`` is off (nexus-2my fix #2).

    Mirrors the git_meta-omitted-when-empty behaviour: drop bib_* whose
    values are zero/empty; keep them when populated.
    """
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "p.pdf",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "bib_year": 0,
        "bib_authors": "",
        "bib_venue": "",
        "bib_citation_count": 0,
    }
    out = normalize(raw, content_type="pdf")
    for key in ("bib_year", "bib_authors", "bib_venue", "bib_citation_count"):
        assert key not in out, f"empty {key} should be dropped"


def test_normalize_keeps_partial_bib_when_year_populated() -> None:
    """If even one bib_* slot has a real value, keep all four for a
    consistent search/display contract."""
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "p.pdf",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "bib_year": 2024,
        "bib_authors": "",
        "bib_venue": "",
        "bib_citation_count": 0,
    }
    out = normalize(raw, content_type="pdf")
    assert out["bib_year"] == 2024
    assert out["bib_authors"] == ""
    assert out["bib_venue"] == ""
    assert out["bib_citation_count"] == 0


def test_normalize_keeps_fully_populated_bib() -> None:
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "p.pdf",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "bib_year": 2024,
        "bib_authors": "Smith",
        "bib_venue": "ICML",
        "bib_citation_count": 42,
    }
    out = normalize(raw, content_type="pdf")
    assert out["bib_year"] == 2024
    assert out["bib_authors"] == "Smith"
    assert out["bib_venue"] == "ICML"
    assert out["bib_citation_count"] == 42


# ── validate() ──────────────────────────────────────────────────────────────


def test_validate_passes_well_formed() -> None:
    from nexus.metadata_schema import normalize, validate

    raw = {
        "source_path": "a",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
    }
    validate(normalize(raw, content_type="code"))  # no raise


def test_validate_rejects_over_cap() -> None:
    from nexus.metadata_schema import MetadataSchemaError, validate

    bloated = {f"k{i}": i for i in range(40)}
    with pytest.raises(MetadataSchemaError, match="too many"):
        validate(bloated)


def test_validate_rejects_unknown_key() -> None:
    from nexus.metadata_schema import MetadataSchemaError, validate

    with pytest.raises(MetadataSchemaError, match="unknown"):
        validate({"source_path": "a", "bogus_key": "x"})


def test_validate_rejects_non_primitive_values() -> None:
    """Chroma metadata only accepts str/int/float/bool/None — enforce upstream."""
    from nexus.metadata_schema import MetadataSchemaError, validate

    with pytest.raises(MetadataSchemaError, match="non-primitive"):
        validate({"source_path": "a", "git_meta": {"branch": "main"}})


# ── Write path protection ───────────────────────────────────────────────────


def test_normalize_output_fits_under_chroma_cap() -> None:
    """Pathological input that superset-ed every historical key still
    produces output under ``MAX_SAFE_TOP_LEVEL_KEYS``."""
    from nexus.metadata_schema import MAX_SAFE_TOP_LEVEL_KEYS, normalize

    raw = {
        # Every historical write-path key
        "source_path": "p.pdf",
        "source_title": "t",
        "source_author": "a",
        "source_date": "2024",
        "section_title": "s",
        "section_type": "body",
        "corpus": "c",
        "store_type": "pdf",
        "category": "prose",
        "tags": "pdf",
        "title": "ti",
        "page_count": 10,
        "page_number": 1,
        "chunk_index": 0,
        "chunk_count": 5,
        "chunk_start_char": 0,
        "chunk_end_char": 100,
        "chunk_type": "text",
        "embedding_model": "voyage-3",
        "indexed_at": "2026-01-01",
        "content_hash": "h",
        "chunk_text_hash": "ht",
        "pdf_subject": "x",
        "pdf_keywords": "y",
        "is_image_pdf": False,
        "has_formulas": True,
        "bib_year": 2024,
        "bib_venue": "ICML",
        "bib_authors": "Smith",
        "bib_citation_count": 10,
        "bib_semantic_scholar_id": "ss",
        "format": "pdf",
        "extraction_method": "docling",
        "filename": "p.pdf",
        "file_extension": ".pdf",
        "programming_language": "",
        "ast_chunked": False,
        "line_start": 0,
        "line_end": 0,
        "session_id": "sess",
        "source_agent": "nexus-indexer",
        "frecency_score": 0.5,
        "expires_at": "",
        "ttl_days": 0,
        "git_project_name": "nexus",
        "git_branch": "main",
        "git_commit_hash": "abc",
        "git_remote_url": "https://example.com",
    }
    out = normalize(raw, content_type="pdf")
    assert len(out) <= MAX_SAFE_TOP_LEVEL_KEYS, (
        f"{len(out)} keys: {sorted(out.keys())}"
    )
