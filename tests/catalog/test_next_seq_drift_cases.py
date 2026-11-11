# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the client's allocator-drift row answers the shared next_seq table.

``nx doctor`` finds owners whose tumbler allocator has fallen behind their own children from the engine route
``GET /v1/catalog/owners/next_seq_drift``, one bounded read with the computation ``POST /owners/sweep_next_seq_drift``
floors by (tombstoned children count, nexus-f3yqd.6). The same rule, written in Python over a list of documents and the
tombstoned tumblers, is the oracle: ``nexus.health._next_seq_drift_rows_over``. The two halves are written in different
languages, so one table (``service/src/test/resources/next-seq-drift-cases.json``) is read by both: the Java
``NextSeqDriftParityTest`` holds the SQL to it (and the sweep to the route), and this test holds the oracle to it, case by
case. ``test_index_run_census_route_parity.py`` then holds the route-fed row to the oracle's. A change to either rule that
the other does not follow fails one of them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import health

_TABLE = Path(__file__).resolve().parents[2] / "service/src/test/resources/next-seq-drift-cases.json"


def _cases() -> list[dict]:
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _owners(case: dict) -> list[dict]:
    """Every owner, deactivated ones too, as the sweep examines them."""
    return [{"tumbler_prefix": o["prefix"], "next_seq": o["next_seq"]} for o in case["owners"]]


def _row(case: dict) -> health.HealthResult:
    walk = [SimpleNamespace(tumbler=t) for t in case["live"]]
    [row] = health._next_seq_drift_rows_over(_owners(case), walk, case["tombstoned"])
    return row


def _view(row: health.HealthResult) -> dict:
    """The drifted owners and the owner count a row's text carries."""
    if m := re.match(r"none \((\d+) owner\(s\) checked", row.detail):
        return {"checked": int(m.group(1)), "drifted": []}
    listing = re.search(r"highest existing tumbler: (.*?)\. The engine floors", row.detail).group(1)
    drifted = [
        {"owner": m.group(1), "next_seq": int(m.group(2)), "highest_child_seq": int(m.group(3))}
        for m in re.finditer(r"(\S+) \(next_seq=(\d+), highest child=(\d+)\)", listing)
    ]
    return {"checked": None, "drifted": sorted(drifted, key=lambda d: d["owner"])}


def test_the_table_is_read_whole() -> None:
    """A table that lost its rows would pass the parametrized test below with nothing to check."""
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(cases) >= 10 and len(set(ids)) == len(ids), ids
    assert sum(1 for c in cases if c["expect"]["drifted"]) >= 6, "too few drifted cases to hold a rule to"
    assert sum(1 for c in cases if not c["expect"]["drifted"]) >= 3, "too few healthy cases to hold a rule to"
    assert any(c["tombstoned"] for c in cases) and any(o["deactivated"] for c in cases for o in c["owners"])


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_the_oracle_row_lists_exactly_the_tables_drifted_owners(case: dict) -> None:
    got = _view(_row(case))
    want = sorted(case["expect"]["drifted"], key=lambda d: d["owner"])
    assert got["drifted"] == want
    if not want:
        assert got["checked"] == case["expect"]["checked"]
