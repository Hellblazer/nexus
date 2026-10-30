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
    assert gen.OUTPUT.read_text(encoding="utf-8") == gen.render(), (
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


def test_a_stale_file_is_caught(tmp_path, monkeypatch):
    """Non-vacuity: the check mode fails on a file that differs from the render."""
    stale = tmp_path / "ChunkMetadataKeys.java"
    stale.write_text(gen.render().replace('"title"', '"titlex"'), encoding="utf-8")
    monkeypatch.setattr(gen, "OUTPUT", stale)
    assert gen.main(["--check"]) == 1
