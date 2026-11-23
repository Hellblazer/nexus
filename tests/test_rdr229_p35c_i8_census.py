# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5c (nexus-p2m1x.25), census invariant I8: the stored-label census is a fresh run, covers every writer, and has no violation.

``service/src/test/resources/rdr229/p35c-i8-stored-label-census.tsv`` has one row per (writer, table) of the P3.1 writer census that writes
a collection name column, with the verdict ``holds`` (the writer files the locked registry row's CURRENT label) or ``n/a`` (not an id-addressed
write of a live collection's row). A writer added to the writer census without a row fails ``scripts/rdr229_p35c_i8_census.py``; a ``violates``
row cannot exist (the site is fixed in the change that finds it).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).parent.parent
_SCRIPT = _REPO / "scripts" / "rdr229_p35c_i8_census.py"
_CENSUS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p35c-i8-stored-label-census.tsv"
_HEADER = ["writer", "table", "sites", "verdict", "basis"]


def _rows() -> list[list[str]]:
    lines = [ln for ln in _CENSUS.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines[0].split("\t") == _HEADER
    return [ln.split("\t") for ln in lines[1:]]


def test_the_committed_census_is_a_fresh_run():
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(_REPO), "--check"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        "the I8 census differs from a fresh run over the writer census; regenerate it with "
        f"`python3 scripts/rdr229_p35c_i8_census.py . --write`:\n{proc.stdout[-1500:]}{proc.stderr[-800:]}"
    )


def test_every_row_has_a_verdict_and_a_basis_and_none_violates():
    rows = _rows()
    assert len(rows) >= 60, f"only {len(rows)} rows: the census stopped seeing the writers"
    for r in rows:
        assert len(r) == 5, r
        assert r[3] in {"holds", "holds-until-P4.1", "n/a"}, f"{r[0]} {r[1]}: verdict {r[3]!r} (a violation is fixed, never listed)"
        assert r[4].strip(), f"{r[0]} {r[1]}: no basis"
    assert len({(r[0], r[1]) for r in rows}) == len(rows), "a (writer, table) listed twice"


def test_the_writers_this_slice_fixed_or_pinned_hold():
    held = {(r[0], r[1]) for r in _rows() if r[3] == "holds"}
    for writer, table in [
        ("TaxonomyRepository.java", "topic_assignments"),   # assign_from_chashes' function argument is the row's current label (P3.5c)
        ("TaxonomyRepository.java", "topics"),
        ("TaxonomyRepository.java", "taxonomy_meta"),
        ("CatalogRepository.java", "catalog_document_chunks"),
        ("ManifestReplace.java", "catalog_document_chunks"),
        ("PgVectorRepository.java", "chunks"),
        ("TaxonomyCentroidRepository.java", "taxonomy_centroids"),
    ]:
        assert (writer, table) in held, (writer, table)
    assert any(w.startswith("nexus.assign_from_chashes_") and t == "topic_assignments" for w, t in held)
