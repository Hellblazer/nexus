# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5b (nexus-p2m1x.24): the committed reader census is a fresh run of its script.

``service/src/test/resources/rdr229/p35b-reader-census.tsv`` lists every live SQL object (function, view, trigger,
policy) whose text reads a collection name column, so P3.5b's surface is known to be complete. A reader added or
redefined without a fresh run fails here; the engine test Rdr229P35bReaderCensusLiveCatalogTest compares the same
object set against pg_catalog of a migrated database, which catches an object this parse misses.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).parent.parent
_SCRIPT = _REPO / "scripts" / "rdr229_p35b_reader_census.py"
_CENSUS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p35b-reader-census.tsv"


def _rows() -> list[list[str]]:
    lines = [ln for ln in _CENSUS.read_text().splitlines() if ln and not ln.startswith("#")]
    return [ln.split("\t") for ln in lines[1:]]


def test_the_committed_census_is_a_fresh_run():
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(_REPO), "--check"], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        "the committed reader census differs from a fresh run; regenerate it with "
        f"`python3 scripts/rdr229_p35b_reader_census.py . --write`:\n{proc.stdout[-2000:]}{proc.stderr[-1000:]}"
    )


def test_the_census_names_the_readers_the_rdr_and_the_fork_run_name():
    rows = _rows()
    assert len(rows) > 200, f"only {len(rows)} census rows: the script stopped seeing the readers"
    objects = {(r[0], r[1]): r[5] for r in rows}
    # the five views P4.1's DROP COLUMN ... CASCADE dropped on the fork (T2 nexus_rdr/229-p32-fork-results-2026-11-20)
    for view in ("collection_health_meta", "collection_doc_counts", "collection_vector_stats", "topics_with_counts",
                 "live_chunks"):
        assert ("view", f"nexus.{view}") in objects, view
    # the two views the RDR names read a chunk-family table, so they read both shapes (form B, no CONTINUE)
    assert objects[("view", "nexus.collection_vector_stats")] == "B"
    assert objects[("view", "nexus.live_chunks")] == "B"
    # catalog-044's trigger fires on a name column, and its function matches catalog_collections.name
    assert ("trigger", "nexus.catalog_documents.catalog_documents_owner_to_collection") in objects
    owner_fn = [r for r in rows if r[1] == "nexus.catalog_documents_owner_to_collection"]
    assert {r[4] for r in owner_fn} >= {"catalog_collections.name", "physical_collection"}


def test_a_write_is_not_a_read():
    rows = _rows()
    # an INSERT's column list and an UPDATE's SET target are P3.1's writer census, never a reader row
    for r in rows:
        assert r[4] in {"collection", "physical_collection", "source_collection", "target_collection", "superseded_by",
                        "catalog_collections.name"}, r
