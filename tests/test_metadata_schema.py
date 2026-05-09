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
    """Every key read by ``where=`` filters or scoring must be top-level.

    RDR-108 Phase 3 retired three chunk-identity keys (``doc_id``,
    ``chunk_index``, ``chunk_count``): the ``document_chunks`` manifest
    table is authoritative and the chunk-level fields became cargo.
    """
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL

    for key in (
        "content_hash",
        "chunk_text_hash",
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
    """RDR-102 D2: ``source_path`` is no longer in ALLOWED_TOP_LEVEL.
    RDR-108 Phase 3: ``chunk_index`` / ``chunk_count`` / ``doc_id`` are
    also dropped. The ``content_hash`` check stands in for "load-bearing
    keys survive"."""
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "/a.pdf",
        "content_hash": "abc",
        "chunk_text_hash": "def",
        "pdf_subject": "should drop",
        "extraction_method": "should drop",
        "ast_chunked": True,
    }
    out = normalize(raw, content_type="pdf")
    assert "pdf_subject" not in out
    assert "extraction_method" not in out
    assert "ast_chunked" not in out
    assert "source_path" not in out, (
        "RDR-102 D2: source_path is no longer in ALLOWED_TOP_LEVEL; "
        "normalize() must drop it"
    )
    assert out["content_hash"] == "abc"


def test_normalize_drops_flat_git_keys() -> None:
    """RDR-101 Phase 5c removed ``git_meta`` from ALLOWED_TOP_LEVEL.
    The flat ``git_*`` keys are dropped at normalize time too — no
    consolidation, no JSON blob, no slot consumed. Catalog Document
    carries git provenance at the document level instead."""
    from nexus.metadata_schema import normalize

    raw = {
        "content_hash": "x",
        "chunk_text_hash": "y",
        "git_project_name": "nexus",
        "git_branch": "main",
        "git_commit_hash": "deadbeef",
        "git_remote_url": "https://github.com/example/nexus.git",
    }
    out = normalize(raw, content_type="code")
    for k in ("git_project_name", "git_branch", "git_commit_hash",
              "git_remote_url", "git_meta"):
        assert k not in out


def test_normalize_injects_content_type() -> None:
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "f.py",
        "content_hash": "x",
        "chunk_text_hash": "y",
    }
    out = normalize(raw, content_type="code")
    assert out["content_type"] == "code"


def test_normalize_rejects_invalid_content_type() -> None:
    from nexus.metadata_schema import normalize

    with pytest.raises(ValueError, match="content_type"):
        normalize({"source_path": "a", "content_hash": "x",
                   "chunk_text_hash": "y"},
                  content_type="binary")


def test_normalize_preserves_bib_fields() -> None:
    from nexus.metadata_schema import normalize

    raw = {
        "source_path": "p.pdf",
        "content_hash": "x",
        "chunk_text_hash": "y",
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
        "git_project_name": "nexus",
        "git_branch": "main",
        "git_commit_hash": "deadbeef",
        "git_remote_url": "https://example.com",
    }
    first = normalize(raw, content_type="code")
    second = normalize(first, content_type="code")
    assert first == second


def test_normalize_idempotent_on_reprocess() -> None:
    """Re-normalising a previously-normalised dict is idempotent.
    RDR-101 Phase 5c: the git_meta unpack/repack round-trip is gone
    (git_meta is no longer in the schema), so this verifies the
    simpler post-5c invariant — round-trip preserves load-bearing
    fields."""
    from nexus.metadata_schema import normalize

    raw = {
        "content_hash": "x",
        "chunk_text_hash": "y",
    }
    first = normalize(raw, content_type="code")
    merged = {**first, "bib_year": 2024, "bib_authors": "Doe"}
    second = normalize(merged, content_type="code")
    assert second["bib_year"] == 2024
    assert second["content_hash"] == "x"
    assert second["content_type"] == "code"


def test_normalize_keeps_ttl_and_indexed_at() -> None:
    """``ttl_days=0`` is the permanent sentinel and is kept; ``expires_at``
    no longer exists in the schema (computed from indexed_at + ttl_days
    via :func:`is_expired` Python-side)."""
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL, normalize

    raw = {
        "source_path": "a",
        "content_hash": "x",
        "chunk_text_hash": "y",
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
        validate({"content_hash": "x", "bogus_key": "x"})


def test_validate_rejects_non_primitive_values() -> None:
    """Chroma metadata only accepts str/int/float/bool/None — enforce upstream.
    Use a key in :data:`ALLOWED_TOP_LEVEL` (so the key-set check passes)
    with a non-primitive value to exercise the type check specifically.
    """
    from nexus.metadata_schema import MetadataSchemaError, validate

    with pytest.raises(MetadataSchemaError, match="non-primitive"):
        validate({"content_hash": "x", "tags": ["list", "instead", "of", "string"]})


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


# ── RDR-101 Phase 3 PR δ — doc_id schema (RETIRED by RDR-108 Phase 3) ────


class TestDocIdRetiredFromChunkSchema:
    """RDR-108 Phase 3 (nexus-bdag) retires ``doc_id`` from chunk-level
    T3 metadata. The catalog ``document_chunks`` manifest table is now
    the authoritative document-to-chunk map; carrying ``doc_id`` on the
    chunk duplicates that state.

    Pre-Phase-3 chunks already in T3 keep the ``doc_id`` field in their
    stored metadata — the schema change only affects new writes.
    Phase 4 retargets the read paths (search/scoring/aspects) to consult
    the manifest directly.
    """

    def test_doc_id_not_in_allowed_top_level(self) -> None:
        from nexus.metadata_schema import ALLOWED_TOP_LEVEL

        assert "doc_id" not in ALLOWED_TOP_LEVEL

    def test_validate_rejects_doc_id_on_new_writes(self) -> None:
        from nexus.metadata_schema import MetadataSchemaError, validate

        meta = {
            "content_hash": "x",
            "chunk_text_hash": "y",
            "content_type": "code",
            "doc_id": "1.1.42",
        }
        with pytest.raises(MetadataSchemaError):
            validate(meta)

    def test_normalize_drops_doc_id(self) -> None:
        """``normalize`` cargo-drops ``doc_id`` (anything outside
        ``ALLOWED_TOP_LEVEL`` is removed silently in the cargo filter)."""
        from nexus.metadata_schema import normalize

        out = normalize(
            {
                "content_hash": "x",
                "chunk_text_hash": "y",
                "doc_id": "1.1.42",
            },
            content_type="code",
        )
        assert "doc_id" not in out

    def test_make_chunk_metadata_rejects_doc_id_kwarg(self) -> None:
        """The ``doc_id=`` kwarg is HARD-REMOVED — passing it must raise
        ``TypeError`` so a regression at any call site is a build break,
        not a silent drop.
        """
        from nexus.metadata_schema import make_chunk_metadata

        with pytest.raises(TypeError):
            make_chunk_metadata(
                content_type="code",
                chunk_text_hash="abc",
                content_hash="def",
                indexed_at="2026-05-09T00:00:00Z",
                embedding_model="voyage-code-3",
                doc_id="1.1.42",
            )


# ── RDR-102 Phase B: source_path retirement ─────────────────────────────


def test_prune_deprecated_keys_disjoint_from_allowed_top_level() -> None:
    """RDR-102 D4 #1 / RF-8: the canonical schema and the prune
    verb's deprecated-key set MUST be disjoint.

    Pre-RDR-102 the intersection was ``{'source_path'}``: every reindex
    rewrote source_path through ``make_chunk_metadata``, the prune verb
    stripped it post-write, the next reindex put it back. The cycle
    only terminates by removing source_path from ALLOWED_TOP_LEVEL —
    Phase B does that. CI failed to catch the original divergence; this
    test makes any future re-introduction a build break.

    The four ``git_*`` keys in _PRUNE_DEPRECATED_KEYS are not in
    ALLOWED_TOP_LEVEL (they get repacked into ``git_meta`` JSON before
    normalize runs), so the prune is structurally one-shot for them
    even today. Only source_path cycles, and only source_path needs
    the schema-level removal.
    """
    from nexus.commands.catalog import _PRUNE_DEPRECATED_KEYS
    from nexus.metadata_schema import ALLOWED_TOP_LEVEL

    intersection = ALLOWED_TOP_LEVEL & _PRUNE_DEPRECATED_KEYS
    assert intersection == frozenset(), (
        f"_PRUNE_DEPRECATED_KEYS and ALLOWED_TOP_LEVEL share keys: "
        f"{sorted(intersection)}. Each shared key creates a regression "
        f"cycle: writer stamps it, prune strips it, writer re-stamps. "
        f"Remove the key from ALLOWED_TOP_LEVEL (and the writer call "
        f"sites that pass it) so normalize() drops it at the source."
    )


def test_make_chunk_metadata_rejects_source_path_kwarg() -> None:
    """RDR-102 D2 / Alternative A3 (REJECTED at substantive-critic
    gate): ``source_path`` is HARD-REMOVED from ``make_chunk_metadata``,
    not kept as a deprecated no-op kwarg. A caller passing
    ``source_path=...`` must get a ``TypeError`` rather than a silent
    drop.

    The silent-drop alternative (A3) was rejected because it is an
    invisible failure mode: caller passes the value, call succeeds,
    value is silently discarded, downstream ``where={"source_path":
    ...}`` returns zero results — the failure surfaces nowhere. The
    hard-remove approach forces every call site to be edited in
    lockstep with a TypeError so re-introduction is a build break.
    """
    import pytest as _pytest

    from nexus.metadata_schema import make_chunk_metadata

    with _pytest.raises(TypeError):
        make_chunk_metadata(
            content_type="code",
            source_path="/should/raise/typeerror.py",  # RDR-102 D2: rejected kwarg
            chunk_text_hash="abc",
            content_hash="def",
            indexed_at="2026-05-02T00:00:00Z",
            embedding_model="voyage-code-3",
            store_type="code",
        )


def test_make_chunk_metadata_does_not_emit_source_path() -> None:
    """RDR-102 D2: a ``make_chunk_metadata`` call with no source_path
    kwarg (the post-Phase-B signature) MUST NOT emit a ``source_path``
    key in the returned metadata. The schema-level removal from
    ALLOWED_TOP_LEVEL is what enforces this — ``normalize()`` drops
    any key not in the set, and source_path no longer is.
    """
    from nexus.metadata_schema import make_chunk_metadata

    meta = make_chunk_metadata(
        content_type="code",
        chunk_text_hash="abc",
        content_hash="def",
        indexed_at="2026-05-02T00:00:00Z",
        embedding_model="voyage-code-3",
    )
    assert "source_path" not in meta, (
        f"source_path must not appear in chunk metadata after Phase B; "
        f"got keys: {sorted(meta.keys())}"
    )


# ── RDR-101 Phase 5c: final schema removal (nexus-o6aa.13) ───────────────────


class TestPhase5cSchemaRemoval:
    """Phase 5c removes 3 deprecated chunk-metadata fields from
    ``ALLOWED_TOP_LEVEL`` entirely: ``corpus``, ``store_type``,
    ``git_meta``. The schema rejects them, the factory refuses the
    kwargs, and ``validate()`` raises on dicts that carry them.

    ``title`` is INTENTIONALLY KEPT — the audit at
    ``docs/migration/rdr-101-phase4-reader-audit.md`` flagged it as
    load-bearing for ``find_ids_by_title`` (the canonical reader
    routing ``nx store delete --title`` and the MCP ``store_get``
    title-fallback path). A deeper analysis on 2026-05-03 confirmed
    the audit's call: dropping title would silently break those
    user-facing surfaces.

    The Phase 5a/5b ``[catalog].event_sourced`` flag machinery is
    REMOVED entirely as part of this phase. The flag was always a
    transitional structure to gate the same drops we're now making
    unconditionally; with the schema doing the work, no flag is
    needed.
    """

    def test_dropped_keys_not_in_allowed_top_level(self) -> None:
        """The 3 Phase 5c keys are gone from the schema. ``title``
        is kept (audit / find_ids_by_title)."""
        from nexus.metadata_schema import ALLOWED_TOP_LEVEL
        for k in ("corpus", "store_type", "git_meta"):
            assert k not in ALLOWED_TOP_LEVEL, (
                f"{k!r} must be removed from ALLOWED_TOP_LEVEL "
                f"(Phase 5c). Current: {sorted(ALLOWED_TOP_LEVEL)}"
            )
        # Title MUST stay — find_ids_by_title is load-bearing.
        assert "title" in ALLOWED_TOP_LEVEL, (
            "title must remain in ALLOWED_TOP_LEVEL — "
            "find_ids_by_title (nx store, MCP store_get) reads it."
        )

    def test_normalize_drops_phase_5c_keys(self) -> None:
        """``normalize()`` drops the 3 keys via the Step 4 cargo
        filter (anything outside ALLOWED_TOP_LEVEL gets dropped)."""
        from nexus.metadata_schema import normalize
        out = normalize(
            {
                "content_type": "code",
                "chunk_text_hash": "abc", "content_hash": "def",
                "indexed_at": "2026-05-03T00:00:00Z",
                "embedding_model": "voyage-code-3",
                "title": "src/foo.py:1-10",
                "corpus": "nexus",
                "store_type": "code",
                "git_project_name": "nexus",
            },
            content_type="code",
        )
        for k in ("corpus", "store_type", "git_meta"):
            assert k not in out, (
                f"{k!r} survived normalize() — Phase 5c should drop it. "
                f"Got keys: {sorted(out.keys())}"
            )
        # Title kept.
        assert out.get("title") == "src/foo.py:1-10"

    def test_make_chunk_metadata_rejects_dropped_kwargs(self) -> None:
        """``corpus``, ``store_type``, ``git_meta`` kwargs are removed
        from the factory signature. Passing any of them raises
        ``TypeError`` — re-introduction is a build break.

        ``title`` kwarg remains accepted.
        """
        from nexus.metadata_schema import make_chunk_metadata
        common_kwargs = dict(
            content_type="code",
            chunk_text_hash="abc", content_hash="def",
            indexed_at="2026-05-03T00:00:00Z",
            embedding_model="voyage-code-3",
        )
        for kwarg, value in [
            ("corpus", "nexus"),
            ("store_type", "code"),
            ("git_meta", {"git_project_name": "nexus"}),
        ]:
            with pytest.raises(TypeError):
                make_chunk_metadata(**common_kwargs, **{kwarg: value})
        # title kwarg still works.
        meta = make_chunk_metadata(**common_kwargs, title="src/foo.py")
        assert meta.get("title") == "src/foo.py"

    def test_validate_rejects_dropped_keys(self) -> None:
        """``validate()`` enforces ALLOWED_TOP_LEVEL membership; the
        3 dropped keys trigger MetadataSchemaError. ``title`` does not."""
        from nexus.metadata_schema import validate, MetadataSchemaError
        good_base = {
            "content_type": "code",
            "chunk_text_hash": "abc", "content_hash": "def",
            "indexed_at": "2026-05-03T00:00:00Z",
            "embedding_model": "voyage-code-3",
        }
        for dropped in ("corpus", "store_type", "git_meta"):
            with pytest.raises(MetadataSchemaError):
                validate({**good_base, dropped: "value"})
        # title stays — must NOT raise.
        validate({**good_base, "title": "src/foo.py"})

    def test_event_sourced_machinery_removed(self) -> None:
        """Phase 5c removes the Phase 5a/5b flag wiring entirely:
        ``_GATED_BY_EVENT_SOURCED`` is gone, ``is_catalog_event_sourced``
        is gone, ``[catalog].event_sourced`` config key is gone. The
        schema enforces the drop unconditionally."""
        import nexus.metadata_schema
        import nexus.config
        assert not hasattr(nexus.metadata_schema, "_GATED_BY_EVENT_SOURCED")
        assert not hasattr(nexus.config, "is_catalog_event_sourced")


# ── RDR-108 Phase 3: chunk-identity removal (nexus-bdag) ─────────────────────


class TestPhase3SchemaRemoval:
    """Phase 3 of RDR-108 retires three chunk-identity fields from
    ``ALLOWED_TOP_LEVEL``: ``doc_id``, ``chunk_index``, ``chunk_count``.
    After Phase 1's ``document_chunks`` manifest table is the catalog's
    authoritative document-to-chunk map, these chunk-level metadata
    keys duplicate manifest state and consume metadata budget for no
    payload.

    After Phase 3:
      * The schema rejects all three (cargo-dropped at normalize, raised
        at validate).
      * The ``make_chunk_metadata`` factory refuses the kwargs (TypeError
        — re-introduction is a build break).
      * Indexer call sites stop emitting them on new writes.
      * Pre-Phase-3 chunks already in T3 keep the fields in stored
        metadata (T3 doesn't strip stored metadata when the schema
        changes); legacy reads still work. Phase 4 rewrites read paths
        to use the manifest.
    """
    PHASE_3_DROPPED = ("doc_id", "chunk_index", "chunk_count")

    def test_dropped_keys_not_in_allowed_top_level(self) -> None:
        from nexus.metadata_schema import ALLOWED_TOP_LEVEL

        for k in self.PHASE_3_DROPPED:
            assert k not in ALLOWED_TOP_LEVEL, (
                f"{k!r} must be removed from ALLOWED_TOP_LEVEL "
                f"(Phase 3). Current: {sorted(ALLOWED_TOP_LEVEL)}"
            )

    def test_normalize_drops_phase_3_keys(self) -> None:
        """``normalize()`` drops the 3 keys via the cargo filter (Step 2:
        anything outside ALLOWED_TOP_LEVEL is dropped silently)."""
        from nexus.metadata_schema import normalize

        out = normalize(
            {
                "content_hash": "x",
                "chunk_text_hash": "y",
                "indexed_at": "2026-05-09T00:00:00Z",
                "embedding_model": "voyage-code-3",
                "doc_id": "1.1.42",
                "chunk_index": 7,
                "chunk_count": 99,
            },
            content_type="code",
        )
        for k in self.PHASE_3_DROPPED:
            assert k not in out, (
                f"{k!r} survived normalize() — Phase 3 should drop it. "
                f"Got keys: {sorted(out.keys())}"
            )

    def test_make_chunk_metadata_rejects_phase_3_kwargs(self) -> None:
        """``doc_id``, ``chunk_index``, ``chunk_count`` kwargs are removed
        from the factory signature. Passing any raises ``TypeError`` —
        re-introduction is a build break.
        """
        from nexus.metadata_schema import make_chunk_metadata

        common_kwargs = dict(
            content_type="code",
            chunk_text_hash="abc",
            content_hash="def",
            indexed_at="2026-05-09T00:00:00Z",
            embedding_model="voyage-code-3",
        )
        for kwarg, value in [
            ("doc_id", "1.1.42"),
            ("chunk_index", 0),
            ("chunk_count", 1),
        ]:
            with pytest.raises(TypeError):
                make_chunk_metadata(**common_kwargs, **{kwarg: value})

    def test_validate_rejects_phase_3_keys(self) -> None:
        """``validate()`` enforces ALLOWED_TOP_LEVEL membership; the 3
        Phase 3 keys trigger MetadataSchemaError."""
        from nexus.metadata_schema import MetadataSchemaError, validate

        good_base = {
            "content_type": "code",
            "chunk_text_hash": "abc",
            "content_hash": "def",
            "indexed_at": "2026-05-09T00:00:00Z",
            "embedding_model": "voyage-code-3",
        }
        for dropped in self.PHASE_3_DROPPED:
            with pytest.raises(MetadataSchemaError):
                validate({**good_base, dropped: "value" if isinstance(dropped, str) and dropped == "doc_id" else 1})

    def test_make_chunk_metadata_does_not_emit_phase_3_keys(self) -> None:
        """A bare ``make_chunk_metadata`` call with the post-Phase-3
        signature must NOT emit any of the 3 dropped keys.
        """
        from nexus.metadata_schema import make_chunk_metadata

        meta = make_chunk_metadata(
            content_type="code",
            chunk_text_hash="abc",
            content_hash="def",
            indexed_at="2026-05-09T00:00:00Z",
            embedding_model="voyage-code-3",
        )
        for k in self.PHASE_3_DROPPED:
            assert k not in meta, (
                f"{k!r} appeared in factory output post-Phase-3; "
                f"got keys: {sorted(meta.keys())}"
            )

    def test_legacy_chunk_dict_reads_unaffected(self) -> None:
        """Pre-Phase-3 chunks stored in T3 still carry doc_id /
        chunk_index / chunk_count in their metadata. Direct ``dict.get``
        reads on a legacy dict continue to return the stored value —
        the schema change only affects new writes via normalize/validate.
        """
        legacy_meta = {
            "content_hash": "x",
            "chunk_text_hash": "y",
            "doc_id": "1.1.42",
            "chunk_index": 3,
            "chunk_count": 10,
        }
        assert legacy_meta.get("doc_id") == "1.1.42"
        assert legacy_meta.get("chunk_index") == 3
        assert legacy_meta.get("chunk_count") == 10
