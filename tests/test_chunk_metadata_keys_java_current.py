# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 0 (nexus-qyx35): the generated Java key set is current.

``ChunkMetadataKeys.java`` is rendered from the key table by
``scripts/gen_chunk_metadata_keys_java.py``. This is the Python half of the pin
(the Java half, ``ChunkMetadataKeysTest``, reads the TOML on its own): a table
edit without a regeneration is red here, on the default loop.
"""
from __future__ import annotations

import gen_chunk_metadata_keys_java as gen  # scripts/ is on the test pythonpath (pyproject)

from nexus.tables import chunk_metadata_keys as ckm


def test_generated_java_equals_what_the_generator_renders_now():
    assert gen.OUTPUT.exists(), f"{gen.OUTPUT} is missing; run the generator with --write"
    assert gen.OUTPUT.read_bytes() == gen.render().encode("utf-8"), (
        "ChunkMetadataKeys.java is stale; run "
        "`uv run python scripts/gen_chunk_metadata_keys_java.py --write`"
    )


def test_generated_java_names_every_metadata_key_of_every_emitted_class():
    text = gen.OUTPUT.read_text(encoding="utf-8")
    examined = 0
    for cls in gen.CLASSES:
        for key in ckm.keys_of(cls):
            assert f'"{key}"' in text, f"{cls} key {key} is not in the generated file"
            examined += 1
    assert examined >= 45, "the sweep examined too few keys to mean anything"
    for prefix in ckm.BYPASS_PREFIXES:
        assert prefix not in text, "a collection-prefix literal would grow CollectionParseGateTest's census"


def test_generated_java_leaves_out_columns_tables_and_catalog_rows():
    text = gen.OUTPUT.read_text(encoding="utf-8")
    for row in ckm.rows():
        if row.kind != "metadata-key":
            assert f'"{row.key}"' not in text, row.key


def test_the_row_wording_separates_the_route_check_from_the_database_check():
    """RDR-236 Phase 4: the ROUTE check admits CHUNK keys only from a client write and refuses or
    logs ROW keys (the engine writes those); the database CHECK admits CHUNK and ROW."""
    text = gen.OUTPUT.read_text(encoding="utf-8")
    assert "ROUTE check admits the" in text and "{@link #CHUNK} keys only" in text
    assert "database CHECK admits" in text
    assert "{@link #ROW}" not in text, "ROW is not emitted, so no link to it"


def test_generated_java_never_names_the_reapers_quarantine_keys():
    """EngineOwnedPredicateIntegrationTest.noJavaMainSourceNamesTheTagKeys forbids any main source
    naming quarantined_by or reaper_quarantined_at outside a comment (the one predicate is
    Routines.reaperOwnsQuarantinedRow). The ROW class is therefore not generated (develop red at
    c94cd4b00, 2026-10-30)."""
    text = gen.OUTPUT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("*", "/*", "//")))
    assert "quarantined_by" not in code and "reaper_quarantined_at" not in code
    assert "ROW" not in gen.CLASSES


def test_a_crlf_file_is_caught(tmp_path, monkeypatch):
    """--check compares bytes: a CRLF-converted but otherwise equal file is stale."""
    crlf = tmp_path / "ChunkMetadataKeys.java"
    crlf.write_bytes(gen.render().replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(gen, "OUTPUT", crlf)
    assert gen.main(["--check"]) == 1


def test_a_stale_file_is_caught(tmp_path, monkeypatch):
    """Non-vacuity: the check mode fails on a file that differs from the render."""
    stale = tmp_path / "ChunkMetadataKeys.java"
    stale.write_text(gen.render().replace('"title"', '"titlex"'), encoding="utf-8")
    monkeypatch.setattr(gen, "OUTPUT", stale)
    assert gen.main(["--check"]) == 1
