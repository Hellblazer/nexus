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
    """The canonical schema fits inside MAX_SAFE_TOP_LEVEL_KEYS.

    RDR-101 Phase 3 PR δ raised MAX_SAFE_TOP_LEVEL_KEYS to 32 (Chroma's
    hard cap) to admit ``doc_id``. Phase 5b will drop legacy
    ``source_path`` (RDR-096 P5.1/P5.2) and restore headroom; until
    then the schema sits AT the cap. The bib_* placeholder-drop and
    git_meta-omitted-when-empty filters in normalize() keep typical
    chunks well under (no-bib + no-git ≈ 26 keys).
    """
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL, MAX_SAFE_TOP_LEVEL_KEYS

    assert len(ALLOWED_TOP_LEVEL) <= MAX_SAFE_TOP_LEVEL_KEYS
    assert MAX_SAFE_TOP_LEVEL_KEYS <= 32, "must not exceed Chroma 32-key cap"


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
        "indexed_at",  # replaces expires_at; expiry derived via is_expired()
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
        # bib_semantic_scholar_id is now allowed — it is the load-bearing
        # "this title was enriched" marker (commands/enrich.py:89,
        # catalog/link_generator.py:38). Without it in the schema,
        # normalize() drops the marker and enrich loses idempotency.
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
        "is_image_pdf",
        "has_formulas",
        "git_project_name",
        "git_branch",
        "git_commit_hash",
        "git_remote_url",
        # Removed in the source_title→title collapse + expires_at→indexed_at swap:
        "source_title",
        "expires_at",
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
        "bib_semantic_scholar_id": "ss-12345",
    }
    out = normalize(raw, content_type="pdf")
    assert out["bib_year"] == 2024
    assert out["bib_authors"] == "Smith, Jones"
    assert out["bib_venue"] == "ICML"
    assert out["bib_citation_count"] == 42
    assert out["bib_semantic_scholar_id"] == "ss-12345"


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


def test_normalize_keeps_ttl_and_indexed_at() -> None:
    """``ttl_days=0`` is the permanent sentinel and is kept; ``expires_at``
    no longer exists in the schema (computed from indexed_at + ttl_days
    via :func:`is_expired` Python-side)."""
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL, normalize

    raw = {
        "source_path": "a",
        "content_hash": "x",
        "chunk_text_hash": "y",
        "chunk_index": 0,
        "chunk_count": 1,
        "ttl_days": 0,
        "indexed_at": "2026-04-26T00:00:00+00:00",
    }
    out = normalize(raw, content_type="code")
    assert out["ttl_days"] == 0
    assert out["indexed_at"] == "2026-04-26T00:00:00+00:00"
    assert "expires_at" not in ALLOWED_TOP_LEVEL


def test_is_expired_uses_indexed_at_plus_ttl() -> None:
    """Replacement for the old expires_at < now WHERE filter."""
    from nexus.metadata_schema import is_expired

    permanent = {"ttl_days": 0, "indexed_at": "2026-01-01T00:00:00+00:00"}
    assert not is_expired(permanent, now_iso="2027-01-01T00:00:00+00:00")

    fresh = {"ttl_days": 30, "indexed_at": "2026-04-20T00:00:00+00:00"}
    assert not is_expired(fresh, now_iso="2026-04-25T00:00:00+00:00")

    stale = {"ttl_days": 30, "indexed_at": "2026-01-01T00:00:00+00:00"}
    assert is_expired(stale, now_iso="2026-04-25T00:00:00+00:00")

    # Missing indexed_at → defensive: don't expire.
    no_idx = {"ttl_days": 30, "indexed_at": ""}
    assert not is_expired(no_idx, now_iso="2026-04-25T00:00:00+00:00")


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


# ── RDR-101 Phase 3 PR δ — doc_id schema support ─────────────────────────


class TestDocIdInSchema:
    """``doc_id`` joins ALLOWED_TOP_LEVEL with drop-when-empty semantics
    parallel to the bib_* and git_meta filters. Live indexing call sites
    populate it from ``Catalog.by_file_path(owner, rel_path).tumbler``;
    call sites that have no Catalog handle pass ``""`` and the field is
    dropped by ``normalize`` so it does not consume a metadata slot.
    """

    def test_doc_id_in_allowed_top_level(self) -> None:
        from nexus.metadata_schema import ALLOWED_TOP_LEVEL

        assert "doc_id" in ALLOWED_TOP_LEVEL

    def test_validate_accepts_doc_id(self) -> None:
        # WITH TEETH: removing ``doc_id`` from ALLOWED_TOP_LEVEL makes
        # ``validate`` raise on any chunk metadata that carries it,
        # which is exactly what blocks the live indexing path under
        # the funnel's ``_write_batch`` validate call.
        from nexus.metadata_schema import validate

        meta = {
            "source_path": "src/foo.py",
            "content_hash": "x",
            "chunk_text_hash": "y",
            "chunk_index": 0,
            "chunk_count": 1,
            "content_type": "code",
            "doc_id": "1.1.42",
        }
        validate(meta)  # must not raise

    def test_make_chunk_metadata_propagates_doc_id(self) -> None:
        # WITH TEETH: confirms make_chunk_metadata wires doc_id into the
        # raw dict and normalize() preserves it. A regression that
        # forgot to add doc_id to the raw assignment would drop the
        # value silently here.
        from nexus.metadata_schema import make_chunk_metadata

        meta = make_chunk_metadata(
            content_type="code",
            source_path="src/foo.py",
            chunk_index=0,
            chunk_count=1,
            chunk_text_hash="abc",
            content_hash="def",
            indexed_at="2026-05-01T00:00:00+00:00",
            embedding_model="voyage-context-3",
            store_type="docs",
            doc_id="1.1.42",
        )
        assert meta["doc_id"] == "1.1.42"

    def test_make_chunk_metadata_drops_empty_doc_id(self) -> None:
        # WITH TEETH: drop-when-empty saves the metadata slot for
        # call sites that don't pass doc_id (back-compat). A regression
        # that left doc_id="" in the dict would consume one of the
        # 32 metadata slots for no payload.
        from nexus.metadata_schema import make_chunk_metadata

        meta = make_chunk_metadata(
            content_type="code",
            source_path="src/foo.py",
            chunk_index=0,
            chunk_count=1,
            chunk_text_hash="abc",
            content_hash="def",
            indexed_at="2026-05-01T00:00:00+00:00",
            embedding_model="voyage-context-3",
            store_type="docs",
            # doc_id omitted (defaults to "")
        )
        assert "doc_id" not in meta

    def test_normalize_drops_explicit_empty_doc_id(self) -> None:
        # WITH TEETH: normalize() Step 4c drops doc_id when value is
        # falsy. Same invariant as bib_* and git_meta drop-when-empty.
        from nexus.metadata_schema import normalize

        out = normalize(
            {
                "source_path": "src/foo.py",
                "content_hash": "x",
                "chunk_text_hash": "y",
                "chunk_index": 0,
                "chunk_count": 1,
                "doc_id": "",
            },
            content_type="code",
        )
        assert "doc_id" not in out

    def test_normalize_preserves_truthy_doc_id(self) -> None:
        # WITH TEETH: drop-when-empty must not also drop populated
        # values. A regression that filtered ``doc_id`` unconditionally
        # would silently lose the catalog cross-reference.
        from nexus.metadata_schema import normalize

        out = normalize(
            {
                "source_path": "src/foo.py",
                "content_hash": "x",
                "chunk_text_hash": "y",
                "chunk_index": 0,
                "chunk_count": 1,
                "doc_id": "1.1.42",
            },
            content_type="code",
        )
        assert out["doc_id"] == "1.1.42"

    def test_normalize_is_idempotent_with_doc_id(self) -> None:
        # Round-trip: re-normalising preserves doc_id without accreting
        # or dropping. Mirrors the bib_* / git_meta idempotency tests.
        from nexus.metadata_schema import normalize

        raw = {
            "source_path": "src/foo.py",
            "content_hash": "x",
            "chunk_text_hash": "y",
            "chunk_index": 0,
            "chunk_count": 1,
            "doc_id": "1.1.42",
        }
        first = normalize(raw, content_type="code")
        second = normalize(first, content_type="code")
        assert first == second
        assert second["doc_id"] == "1.1.42"
