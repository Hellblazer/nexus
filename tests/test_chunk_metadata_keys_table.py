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

import dataclasses
import inspect
import re
import tomllib
from importlib import resources

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
        assert r.remedy_reader == "" or r.remedy_reader in ckm.REMEDIES, r
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
        embedding_model="bge-base-en-v1.5",
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


def test_reader_remedies_are_pinned():
    """Where the writer's remedy and the reader's differ the row carries both (the
    census 2/2 section 5 split): a change here is deliberate and cites the census."""
    got = {r.key: (r.remedy, r.remedy_reader) for r in ckm.rows() if r.remedy_reader}
    assert got == {
        "title": ("R1", "R5"),
        "catalog_doc_id": ("R2", "R5"),
        "doc_id": ("R2", "R5"),
        "content_hash": ("R2", "R5"),
    }


def test_the_remedies_the_census_does_not_give_are_recorded_as_the_plans():
    for key in ("source_author", "category", "source_agent"):
        row = ckm.row_for(key)
        assert row.remedy == "R3"
        assert "plan 6pfgb" in row.moved_by and "not the census" in row.moved_by, row
    frec = ckm.row_for("nexus.frecency")
    assert frec.remedy == "n/a" and "6pfgb.42" in frec.moved_by and "unconfirmed" in frec.moved_by
    assert "R6" in ckm.row_for("title").moved_by


def test_a_row_missing_an_emit_field_is_refused_by_name(monkeypatch):
    """A missing emit field names the row and the field (a ValueError), not a bare KeyError."""
    table = ckm.load_table()
    first = table.rows[0]
    broken = dataclasses.replace(first, outcome={k: v for k, v in first.outcome.items() if k != "home"})
    patched = dataclasses.replace(table, rows=(broken, *table.rows[1:]))
    monkeypatch.setattr(ckm, "load_table", lambda: patched)
    ckm.rows.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"chunk_text_hash.*home"):
            ckm.rows()
    finally:
        ckm.rows.cache_clear()


# ---- the Java pin's parser preconditions (ChunkMetadataKeysTest reads the file by lines) ----

_JAVA_EMIT_FIELD = re.compile(r'\b(class|kind)\s*=\s*"([^"]*)"')


def _table_text() -> str:
    return resources.files("nexus.tables").joinpath(ckm.TABLE_RESOURCE).read_bytes().decode("utf-8")


def _raw_blocks() -> list[list[str]]:
    """The non-comment lines of each ``[[row]]`` block, stripped."""
    blocks: list[list[str]] = []
    for line in _table_text().splitlines():
        s = line.strip()
        if s == "[[row]]":
            blocks.append([])
        elif blocks and s and not s.startswith("#"):
            blocks[-1].append(s)
    return blocks


def test_every_row_is_one_match_line_then_one_emit_line():
    """ChunkMetadataKeysTest pairs a single-line match with the single-line emit after it. The
    Java job is path-filtered to service/**, so a TOML-only edit that breaks this would surface
    on the next unrelated service/ change; the default loop holds it instead."""
    blocks = _raw_blocks()
    assert len(blocks) >= 30, "the sweep examined too few rows to mean anything"
    for block in blocks:
        assert [ln.split("=")[0].strip() for ln in block] == ["id", "match", "emit"], block
        assert block[1].endswith("}") and block[2].endswith("}"), block


def test_the_java_pin_reads_each_rows_class_and_kind_from_its_emit_line():
    """The Java parser takes the class/kind strings found anywhere on the emit line, free text
    included, so no home or moved_by may contain ``class = "`` or ``kind = "``."""
    raw = tomllib.loads(_table_text())
    for row, block in zip(raw["row"], _raw_blocks(), strict=True):
        emit = row["emit"]
        for field in ("home", "moved_by"):
            assert not _JAVA_EMIT_FIELD.search(emit[field]), (row["id"], field)
        assert dict(_JAVA_EMIT_FIELD.findall(block[2])) == {"class": emit["class"], "kind": emit["kind"]}, row["id"]
