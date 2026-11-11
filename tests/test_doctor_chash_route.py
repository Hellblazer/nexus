# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): the managed chash conformance row reads the engine's constraint state when it has one (0.1.189).

Validated width CHECKs on both covered tables answer the row clean with no scan (about 7 s on a laptop tenant). Any
other answer, an unvalidated store (every store until the VALIDATE job runs), no route, a refusal or a failed read,
scans as before and adds no warning text, so a fresh install's doctor is unchanged.
"""
from __future__ import annotations

from typing import Any

import pytest

import nexus.health as h


class _Cat:
    def __init__(self, validated: Any) -> None:
        self.validated = validated
        self.dims: list[int] = []

    def chash_constraints_validated(self) -> Any:
        if isinstance(self.validated, Exception):
            raise self.validated
        return self.validated

    def chash_conformance_report(self, dim: int) -> dict:
        self.dims.append(dim)
        return {"dim": dim, "tables": [{"table_name": f"chunks_{dim}", "total": 5, "non_conformant": 0, "sample_chashes": []}]}


def _row(monkeypatch: pytest.MonkeyPatch, cat: _Cat) -> h.HealthResult:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat)
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: type("T3", (), {"list_collections": lambda self: []})())
    return h._check_chash_conformance_report()[0]


def test_validated_constraints_answer_clean_without_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _Cat(True)
    r = _row(monkeypatch, cat)
    assert cat.dims == [] and r.ok is True and r.detail.startswith("clean: the chash width CHECKs"), r.detail


@pytest.mark.parametrize("validated", [False, None, RuntimeError("route gone")])
def test_anything_else_scans_as_before_with_no_warning(monkeypatch: pytest.MonkeyPatch, validated: Any) -> None:
    cat = _Cat(validated)
    r = _row(monkeypatch, cat)
    assert cat.dims == list(h._CHASH_CONFORMANCE_REPORT_DIMS)
    assert r.ok is True and r.warn is False, r.detail
    assert "constraint state" not in r.detail and "validated" not in r.detail, "the fallback names the skipped read"
