# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shakedown 7.94.0 class A: ``nx doctor`` did not finish on a managed tenant of 43.7k catalog documents.

Three full catalog walks ran through the paged HTTP client in one doctor run: the stale-index-run row
and the failed-run row shared one, the allocator-drift row made its own, and the pending-rungs row
reached the manifest-section-labels rung's ``detect()``, whose dry run lists every document and reads
manifests and T3 per batch (a 15-minute walk of shared capacity; nexus-75w5d refused it for ``nx upgrade
--auto`` and left doctor walking). The three index-run rows now read the engine's census routes (nexus-f3yqd.6),
so neither a default run nor ``--check-index-runs`` walks.

These tests run the real ``run_health_checks`` with every other row stubbed, over a fake catalog of
43,700 documents that counts ``all_documents`` calls and the pages the HTTP client would fetch. The fake has no
census routes, so the three rows report not checked and say what they need.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from nexus import health
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS, LadderRegistry
from nexus.upgrade_ladder.rungs.manifest_section_labels import ManifestSectionLabelsRung

DOCUMENTS = 43_700
PAGE = 1000  # the HTTP client's page size for a full listing


class CountingCatalog:
    """A catalog of ``DOCUMENTS`` documents; ``all_documents`` counts calls and the pages a paged client reads."""

    def __init__(self, n: int = DOCUMENTS) -> None:
        self.n = n
        self.calls = 0
        self.pages = 0
        self.stats_calls = 0

    def all_documents(self, limit: int = 0, **_kw):
        self.calls += 1
        self.pages += math.ceil(self.n / PAGE)
        return [
            SimpleNamespace(
                tumbler=f"1.2.{i + 1}", index_state="indexed", index_state_reported=True,
                indexed_at="2026-10-01T00:00:00Z", alias_of="", content_type="docs", physical_collection="docs__1-2",
            )
            for i in range(self.n)
        ]

    def list_owners(self, include_deactivated: bool = False):
        return [{"tumbler_prefix": "1.2", "next_seq": self.n}]

    def stats(self) -> dict:
        self.stats_calls += 1
        return {"doc_count": self.n, "chunk_count": self.n * 3}

    def get_manifests(self, *_a, **_k):  # pragma: no cover - a call here is the failure under test
        raise AssertionError("the rung scan read manifests")

    def close(self) -> None:
        pass



class _NoCollections:
    def list_collections(self, *a, **k) -> list:
        return []


KEEP = ("_check_index_run_routes", "_check_pending_rungs")


def _doctor_over(monkeypatch: pytest.MonkeyPatch, cat: CountingCatalog, *, managed: bool, rung_reads: list,
                 index_runs: bool = False):
    """Run ``run_health_checks`` with only the catalog-reading rows live, over *cat*."""
    for name in dir(health):
        if name.startswith("_check_") and name not in KEEP and callable(getattr(health, name)):
            monkeypatch.setattr(health, name, lambda *a, **k: [])
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _NoCollections())
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat)

    def reader_for_rung():
        rung_reads.append(1)
        return cat

    rung = ManifestSectionLabelsRung(
        reader_factory=reader_for_rung, writer_factory=lambda: None, t3_factory=lambda: None,
        record_fn=lambda: None, managed_fn=lambda: managed, stale_zero_fn=lambda: False,
    )
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))
    # Neither the default sweep nor `nx doctor --check-index-runs` walks (F30): the three rows read the census routes.
    results = health.run_index_run_checks() if index_runs else health.run_health_checks()[0]
    return {r.label: r for r in results}


@pytest.mark.usefixtures("cloud_mode")
def test_a_default_doctor_run_does_not_walk_the_catalog(monkeypatch) -> None:
    """F30 (nexus-f3yqd.6): the default sweep reads the census routes (here an engine without them) instead of every document."""
    cat = CountingCatalog()
    rows = _doctor_over(monkeypatch, cat, managed=True, rung_reads=[])
    assert cat.calls == 0, f"{cat.calls} full catalog walks in a default doctor run"
    for label in ("stale index-run fences", "failed index runs with hidden chunks", "tumbler allocator drift"):
        assert rows[label].ok is True and rows[label].skipped is True and not rows[label].warn, rows[label]
        assert "needs engine 0.1.190 or later" in rows[label].detail, rows[label]
    # The rung's dry run lists every document and reads manifests: on a managed service doctor skips it.
    assert cat.stats_calls == 0
    ladder = rows["Upgrade ladder"]
    assert ladder.warn and not ladder.ok
    assert "not checked" in ladder.detail and RUNG_MANIFEST_SECTION_LABELS in ladder.detail
    assert any("nx upgrade" in s and "interactively" in s for s in ladder.fix_suggestions), ladder.fix_suggestions


@pytest.mark.usefixtures("cloud_mode")
def test_check_index_runs_does_not_walk_the_catalog_either(monkeypatch) -> None:
    cat = CountingCatalog()
    rows = _doctor_over(monkeypatch, cat, managed=True, rung_reads=[], index_runs=True)
    assert cat.calls == 0, f"{cat.calls} full catalog walks in one --check-index-runs run"
    for label in ("stale index-run fences", "failed index runs with hidden chunks", "tumbler allocator drift"):
        assert rows[label].skipped is True and "needs engine 0.1.190 or later" in rows[label].detail, rows[label]


def test_a_local_engine_does_not_scan_in_doctor_either(monkeypatch) -> None:
    """nexus-mjmjl: doctor makes only bounded reads, on a local engine too (a 43k-document local store spent
    minutes in this scan); the rung is reported not checked and nothing is walked."""
    class LocalCatalog(CountingCatalog):
        def get_manifests(self, *_a, **_k):
            return {}

    cat = LocalCatalog(n=10)
    rung = ManifestSectionLabelsRung(
        reader_factory=lambda: cat, writer_factory=lambda: None, t3_factory=lambda: None,
        record_fn=lambda: None, managed_fn=lambda: False, stale_zero_fn=lambda: False,
    )
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))
    (row,) = health._check_pending_rungs()
    assert cat.calls == 0, "doctor walked the catalog on a local engine"
    assert row.warn and not row.ok and "not checked" in row.detail
