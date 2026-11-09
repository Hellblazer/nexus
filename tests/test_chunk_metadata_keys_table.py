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
    """The RDR's key table has 56 keys over seven classes; a change is deliberate."""
    counts = {c: sum(1 for r in ckm.rows() if r.cls == c) for c in ckm.CLASSES}
    assert counts == {
        "CHUNK": 2,
        "DOCUMENT": 25,
        "POSITION": 7,
        "ROW": 7,
        "LEGACY": 12,
        "NOT_ROW": 4,
        "STAMP": 1,
    }


def test_allowed_top_level_is_the_tables_chunk_rows():
    """RDR-236 Phase 4 Step 1 (nexus-6pfgb.65): the client write allowlist is the table's CHUNK rows and nothing
    else; the DOCUMENT and POSITION rows left it (their homes are the catalog document and the manifest row)."""
    assert ALLOWED_TOP_LEVEL == ckm.keys_of("CHUNK") == frozenset({"chunk_text_hash", "embedding_model"})
    assert not ALLOWED_TOP_LEVEL & (ckm.keys_of("DOCUMENT") | ckm.keys_of("POSITION") | ckm.keys_of("LEGACY"))


def test_the_on_row_column_agrees_with_the_write_allowlist():
    """``on_row`` says where a key stands against ``ALLOWED_TOP_LEVEL``: ``allowed`` is exactly the allowlist (the
    metadata-key rows), and no live client writer sends a key past it, so no row is ``unvalidated`` and every
    DOCUMENT, POSITION and LEGACY metadata key is ``stored`` (on old rows only) or ``absent`` (never on a row)."""
    allowed = {r.key for r in ckm.rows() if r.kind == "metadata-key" and r.on_row == "allowed"}
    assert allowed == ALLOWED_TOP_LEVEL, (
        f"on_row = allowed only in the table: {sorted(allowed - ALLOWED_TOP_LEVEL)}; "
        f"in ALLOWED_TOP_LEVEL only: {sorted(ALLOWED_TOP_LEVEL - allowed)}")
    assert [r.key for r in ckm.rows() if r.on_row == "unvalidated"] == []
    non_chunk = [r for r in ckm.rows() if r.kind == "metadata-key" and r.cls in ("DOCUMENT", "POSITION", "LEGACY")]
    assert len(non_chunk) >= 40, "the table examined too little to mean anything"
    assert {r.on_row for r in non_chunk} <= {"stored", "absent"}, sorted(
        (r.key, r.on_row) for r in non_chunk if r.on_row not in ("stored", "absent"))


def test_make_chunk_metadata_output_equals_the_tables_chunk_rows():
    out = make_chunk_metadata(content_type="pdf", chunk_text_hash="a" * 64, embedding_model="bge-base-en-v1.5")
    assert frozenset(out) == ckm.keys_of("CHUNK")


def test_make_chunk_metadata_parameters_are_chunk_keys_and_the_validated_content_type():
    """A new kwarg on the factory is a new key; it needs a CHUNK table row first. ``content_type`` is the
    writer's routing argument, validated and not written."""
    params = frozenset(inspect.signature(make_chunk_metadata).parameters)
    assert params == ckm.keys_of("CHUNK") | {"content_type"}


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


def test_bypass_prefixes_equal_the_write_path_exemption():
    """Inconsistency 2: one named copy, pinned to the code that applies it."""
    assert ckm.BYPASS_PREFIXES == tuple(_BYPASS_SCHEMA_PREFIXES)
    assert ckm.BYPASS_PREFIXES, "an empty prefix list would exempt nothing and pass"


def test_row_keys_are_the_engine_written_quarantine_keys():
    assert ckm.keys_of("ROW") == frozenset(
        {"quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at"}
    )


def test_the_quarantine_retention_stamp_is_a_stamp_not_an_engine_owned_row_key():
    """nexus-alsq3: ``quarantine_expires_at`` is written by the engine on quarantine-* rows only. It stays out of
    the ROW set, so the key-retirement plan an old client renders (and the digest an old engine holds) is unchanged in
    both directions, and out of the allow-list the CHECK and the sweep read."""
    assert ckm.keys_of("STAMP") == frozenset({"quarantine_expires_at"})
    plan = ckm.engine_plan()
    assert "quarantine_expires_at" not in plan["allowed_keys"]
    assert "quarantine_expires_at" not in ckm.keys_of("ROW") | ckm.keys_of("CHUNK")
    assert "quarantine_expires_at" not in ALLOWED_TOP_LEVEL
    assert ckm.row_for("quarantine_expires_at").on_row == "engine"


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
