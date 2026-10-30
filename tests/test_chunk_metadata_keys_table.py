# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 0 (nexus-qyx35): the chunk-metadata key table is true today.

The table (``src/nexus/tables/chunk-metadata-keys.toml``) is the checked
form of the RDR's key table. These tests hold it to the code before anything
moves: ``ALLOWED_TOP_LEVEL`` and ``make_chunk_metadata`` equal its allowed
CHUNK, DOCUMENT and POSITION keys, so a key added to either side without a
table row is red, and a table row that claims a key the writer vocabulary
does not hold is red too.
"""
from __future__ import annotations

import inspect

import pytest

from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES
from nexus.metadata_schema import ALLOWED_TOP_LEVEL, make_chunk_metadata, normalize
from nexus.tables import chunk_metadata_keys as ckm
from nexus.tables.check import BLOCKING_CODES, check_table

_ROW_CLASSES = ("CHUNK", "DOCUMENT", "POSITION")


def _table_allowed() -> frozenset[str]:
    """CHUNK, DOCUMENT and POSITION keys the write-time allowlist holds."""
    return frozenset(
        r.key for r in ckm.rows() if r.cls in _ROW_CLASSES and r.on_row == "allowed"
    )


def test_table_lints_clean_under_the_house_checker():
    findings = [f for f in check_table(ckm.load_table()) if f.code in BLOCKING_CODES]
    assert findings == []


def test_every_declared_key_has_exactly_one_row():
    declared = ckm.load_table().dimensions["key"].domain
    keys = [r.key for r in ckm.rows()]
    assert len(declared) >= 50, "the sweep examined too little to mean anything"
    assert sorted(keys) == sorted(declared)
    assert len(keys) == len(set(keys))


def test_row_vocabulary_is_closed():
    for r in ckm.rows():
        assert r.cls in ckm.CLASSES, r
        assert r.kind in ckm.KINDS, r
        assert r.on_row in ckm.ON_ROW, r
        assert r.remedy in ckm.REMEDIES, r
        assert r.home and r.moved_by, r


def test_row_counts_per_class_are_pinned():
    """The RDR's key table has 56 keys over six classes; a change is deliberate."""
    counts = {c: sum(1 for r in ckm.rows() if r.cls == c) for c in ckm.CLASSES}
    assert counts == {
        "CHUNK": 2,
        "DOCUMENT": 24,
        "POSITION": 7,
        "ROW": 7,
        "LEGACY": 12,
        "NOT_ROW": 4,
    }


def test_allowed_top_level_equals_the_tables_allowed_rows():
    assert ALLOWED_TOP_LEVEL == _table_allowed(), (
        "ALLOWED_TOP_LEVEL and the key table disagree. Added to the code only: "
        f"{sorted(ALLOWED_TOP_LEVEL - _table_allowed())}. Added to the table "
        f"only: {sorted(_table_allowed() - ALLOWED_TOP_LEVEL)}. Edit "
        "src/nexus/tables/chunk-metadata-keys.toml with the code (RDR-236)."
    )


def test_make_chunk_metadata_output_equals_the_tables_allowed_rows():
    """With every optional field set to a non-default value, the factory emits
    exactly the allowed keys (normalize drops the empty defaults)."""
    out = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-10-30T00:00:00+00:00",
        embedding_model="voyage-context-3",
        chunk_start_char=1, chunk_end_char=2, line_start=3, line_end=4, page_number=5,
        title="t", source_author="a", section_title="s", section_type="body",
        tags="x", category="paper",
        bib_year=2020, bib_authors="b", bib_venue="v", bib_citation_count=3,
        bib_semantic_scholar_id="id",
        ttl_days=7, frecency_score=0.5, source_agent="agent", session_id="sess",
        extraction_source="dt_content", extraction_method="mineru",
        quality_gate_overridden=True,
    )
    assert frozenset(out) == _table_allowed()


def test_make_chunk_metadata_parameters_are_table_keys():
    """A new kwarg on the factory is a new key; it needs a table row first."""
    params = frozenset(inspect.signature(make_chunk_metadata).parameters)
    assert params == _table_allowed()


@pytest.mark.parametrize(
    "key",
    ["doc_id", "git_project_name", "git_branch", "git_commit_hash", "git_remote_url", "chunk_type"],
)
def test_normalize_strips_the_legacy_keys_when_it_is_called(key: str):
    """Settled with RDR-236 Phase 0: normalize drops these. Production's repo
    PDF path never called it (see the table's header), which is why the writer,
    not normalize, is where the git keys are removed."""
    assert key in {r.key for r in ckm.rows() if r.cls == "LEGACY"}
    out = normalize({key: "x", "title": "t"}, content_type="prose")
    assert key not in out


def test_unvalidated_document_keys_are_not_in_the_write_allowlist():
    """Inconsistency 1: the table records the keys that bypass ALLOWED_TOP_LEVEL
    instead of widening the list Phase 4 shrinks."""
    unvalidated = {r.key for r in ckm.rows() if r.on_row == "unvalidated"}
    assert unvalidated == {
        "bib_openalex_id", "bib_doi", "catalog_doc_id", "chunk_type", "doc_id",
    }
    assert not unvalidated & ALLOWED_TOP_LEVEL


def test_bypass_prefixes_equal_the_write_path_exemption():
    """Inconsistency 2: one named copy, pinned to the code that applies it."""
    assert ckm.BYPASS_PREFIXES == tuple(_BYPASS_SCHEMA_PREFIXES)
    assert ckm.BYPASS_PREFIXES, "an empty prefix list would exempt nothing and pass"


def test_row_keys_are_the_engine_written_quarantine_keys():
    assert ckm.keys_of("ROW") == frozenset(
        {"quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at"}
    )


def test_non_chunk_metadata_keys_is_document_position_and_legacy():
    got = ckm.non_chunk_row_keys()
    assert got == (
        ckm.keys_of("DOCUMENT") | ckm.keys_of("POSITION") | ckm.keys_of("LEGACY")
    )
    assert not got & ckm.keys_of("CHUNK")
    assert not got & ckm.keys_of("ROW")
