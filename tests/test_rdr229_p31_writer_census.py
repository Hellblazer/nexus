# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17), code review F2: the committed writer census is a fresh run of its script.

``service/src/test/resources/rdr229/p31-writer-census.tsv`` lists every write site that must set ``collection_id`` (jOOQ writes
in service/src/main/java and the live SQL functions of the changelog). Nothing enforced ``--check``, so it went stale after the
last commit that moved a line, and a writer added without its row (``CatalogRepository.rederiveOwner``'s no-op owner write)
went unseen. A stale file also feeds ``P31SqlDualWriteSupport`` the wrong sql rows. The script is the one source: this runs it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).parent.parent
_SCRIPT = _REPO / "scripts" / "rdr229_p31_writer_census.py"
_CENSUS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p31-writer-census.tsv"


def test_the_committed_census_is_a_fresh_run():
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(_REPO), "--check"], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        "the committed census differs from a fresh run; regenerate it with "
        f"`python3 scripts/rdr229_p31_writer_census.py . --write`:\n{proc.stdout[-2000:]}{proc.stderr[-1000:]}"
    )


def test_the_census_is_not_empty_and_names_the_owner_rederivation():
    rows = [ln for ln in _CENSUS.read_text().splitlines() if ln and not ln.startswith("#")]
    assert len(rows) > 150, f"only {len(rows)} census rows: the script stopped seeing the writers"
    assert any("CatalogRepository.java" in ln and "catalog_collections" in ln for ln in rows)
