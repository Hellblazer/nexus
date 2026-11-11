# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the client's allocator-drift row answers the shared next_seq table.

``nx doctor`` finds owners whose tumbler allocator has fallen behind their own children with
``nexus.health._check_next_seq_drift`` (a walk of every live document and the trash list; tombstoned children count, as
the engine's floor does, nexus-f3yqd.6). The engine route ``GET /v1/catalog/owners/next_seq_drift`` answers the same
question in one bounded read, with the computation ``POST /owners/sweep_next_seq_drift`` floors by. The two halves are
written in different languages, so one table (``service/src/test/resources/next-seq-drift-cases.json``) is read by both:
the Java ``NextSeqDriftParityTest`` holds the SQL to it (and the sweep to the route), and this test holds the client row to
it, case by case, through the real function a doctor run calls. A change to either rule that the other does not follow
fails one of the two.
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


class _Catalog:
    """The owners (deactivated ones too, as the row asks) and the trash list, paged, as the catalog answers them."""

    def __init__(self, case: dict) -> None:
        self._owners = [{"tumbler_prefix": o["prefix"], "next_seq": o["next_seq"]} for o in case["owners"]]
        self._trash = [{"tumbler": t} for t in case["tombstoned"]]

    def list_owners(self, include_deactivated: bool = False) -> list:
        assert include_deactivated, "the row covers deactivated owners, as the sweep does"
        return self._owners

    def list_trash(self, *, limit: int, offset: int) -> list:
        return self._trash[offset:offset + limit]


def _row(monkeypatch: pytest.MonkeyPatch, case: dict) -> health.HealthResult:
    from nexus.catalog import factory

    monkeypatch.setattr(factory, "make_catalog_reader", lambda *a, **k: _Catalog(case))
    walk = [SimpleNamespace(tumbler=t) for t in case["live"]]
    [row] = health._check_next_seq_drift(walk)
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
def test_the_client_row_lists_exactly_the_tables_drifted_owners(case: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    got = _view(_row(monkeypatch, case))
    want = sorted(case["expect"]["drifted"], key=lambda d: d["owner"])
    assert got["drifted"] == want
    if not want:
        assert got["checked"] == case["expect"]["checked"]
