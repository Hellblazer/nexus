# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wbfpw.19 (RDR-192 Step 10, client half): ``nx store list --reapable`` against a REAL engine.

Lists exactly the reapable rows of the S1b fixture (``tests/_reapable_cli_fixture.py``) and prints
the empty line on a clean collection.
"""
from __future__ import annotations

import re

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests._catalog_fixture_ops import ActiveCatalog
from tests._reapable_age import age_chunks_past_grace
from tests._reapable_cli_fixture import build_mixed_collection, collection_name, write_chunks

# Not integration-marked: the substrate provisions itself (nexus-wbfpw.38).


@pytest.fixture
def env(t2_service_env):
    return ActiveCatalog(), CliRunner()


def test_lists_exactly_the_reapable_rows_of_the_fixture(env):
    cat, runner = env
    fx = build_mixed_collection(cat, "wbfpw19-list")

    result = runner.invoke(main, ["store", "list", "--reapable", "-c", fx.name])
    assert result.exit_code == 0, result.output

    listed = {c for c in fx.everything if c in result.output}
    assert listed == fx.reapable, f"listed {listed}, expected exactly {fx.reapable}\n{result.output}"
    for chash in fx.reapable:
        line = next(line for line in result.output.splitlines() if chash in line)
        assert "wbfpw19-list orphan" in line  # the chunk's title, from its own metadata
        assert re.search(r"\b\d+d\b", line), line  # age in days, from last_written_at
        assert "Z" in line  # the timestamps, as the engine reports them
    # Read-only: listing moved nothing.
    from nexus.db.http_vector_client import HttpVectorClient

    client = HttpVectorClient()
    assert set(client.get_collection(fx.name).get_all_metadata(include_non_live=True)["ids"]) == fx.everything


def test_a_clean_collection_prints_the_empty_line(env):
    cat, runner = env
    coll = collection_name("wbfpw19-clean")
    owner = cat.register_owner("wbfpw19-clean-owner", "curator")
    (kept,) = write_chunks(coll, ["wbfpw19-clean owned chunk."])
    doc = cat.register(owner, "wbfpw19-clean-doc", content_type="knowledge",
                       physical_collection=coll, meta={"doc_id": kept})
    cat.append_manifest_chunks(str(doc), [{"chash": kept, "position": 0}], collection=coll)
    cat.resync_chunk_count_cache(str(doc))
    age_chunks_past_grace(coll)

    result = runner.invoke(main, ["store", "list", "--reapable", "-c", coll])
    assert result.exit_code == 0, result.output
    assert f"0 reapable chunks in {coll}" in result.output


def test_an_unknown_collection_is_refused_not_reported_clean(env):
    """The engine answers the listing for any name with an empty 200; the verb must not turn a typo
    into "0 reapable chunks", the words a clean collection gets."""
    _cat, runner = env
    typo = collection_name("wbfpw19-no-such-collection")
    result = runner.invoke(main, ["store", "list", "--reapable", "-c", typo])
    assert result.exit_code == 1, result.output
    assert "no collection named" in result.output
    assert "0 reapable chunks" not in result.output


def test_without_collection_is_a_usage_error_before_any_engine_call(env):
    _cat, runner = env
    result = runner.invoke(main, ["store", "list", "--reapable"])
    assert result.exit_code == 2, result.output
    assert "--reapable requires --collection" in result.output
