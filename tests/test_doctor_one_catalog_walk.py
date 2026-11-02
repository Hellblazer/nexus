# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shakedown 7.94.0 class A: ``nx doctor`` did not finish on a managed tenant of 43.7k catalog documents.

Three full catalog walks ran through the paged HTTP client in one doctor run: the stale-index-run row
and the failed-run row shared one, ``_check_next_seq_drift`` made its own, and the pending-rungs row
reached the manifest-section-labels rung's ``detect()``, whose dry run lists every document and reads
manifests and T3 per batch (a 15-minute walk of shared capacity; nexus-75w5d refused it for ``nx upgrade
--auto`` and left doctor walking).

These tests run the real ``run_health_checks`` with every other row stubbed, over a fake catalog of
43,700 documents that counts ``all_documents`` calls and the pages the HTTP client would fetch.
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


class _NoMemo:
    def load(self):
        return None

    def save(self, _note) -> None:
        pass

    def clear(self) -> None:
        pass


class _NoCollections:
    def list_collections(self, *a, **k) -> list:
        return []


KEEP = ("_check_stale_indexing_runs", "_check_failed_runs_hidden_chunks", "_check_next_seq_drift", "_check_pending_rungs")


def _doctor_over(monkeypatch: pytest.MonkeyPatch, cat: CountingCatalog, *, managed: bool, rung_reads: list):
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
        record_fn=lambda: None, memo_store=_NoMemo(), managed_fn=lambda: managed,
    )
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))
    results, _local = health.run_health_checks()
    return {r.label: r for r in results}


@pytest.mark.usefixtures("cloud_mode")
def test_a_managed_doctor_run_walks_the_catalog_once_and_not_through_the_rung(monkeypatch) -> None:
    cat = CountingCatalog()
    rung_reads: list = []
    rows = _doctor_over(monkeypatch, cat, managed=True, rung_reads=rung_reads)
    # One shared walk feeds the stale-run row, the failed-run row and the allocator-drift row.
    assert cat.calls == 1, f"{cat.calls} full catalog walks in one doctor run"
    assert cat.pages == math.ceil(DOCUMENTS / PAGE)
    # The rung's dry run lists every document and reads manifests: on a managed service doctor skips it.
    assert cat.stats_calls == 0
    ladder = rows["Upgrade ladder"]
    assert ladder.warn and not ladder.ok
    assert "not checked" in ladder.detail and RUNG_MANIFEST_SECTION_LABELS in ladder.detail
    assert any("nx upgrade" in s and "interactive" in s for s in ladder.fix_suggestions), ladder.fix_suggestions
    # The three shared rows still answered from the one walk (non-vacuous: they read the list, not skipped).
    assert rows["stale index-run fences"].detail and "skipped" not in rows["stale index-run fences"].detail
    assert "skipped" not in rows["tumbler allocator drift"].detail


@pytest.mark.usefixtures("cloud_mode")
def test_a_failed_shared_walk_is_not_walked_again_by_each_row(monkeypatch) -> None:
    class FailingCatalog(CountingCatalog):
        def all_documents(self, limit: int = 0, **_kw):
            self.calls += 1
            raise RuntimeError("edge saturated")

    cat = FailingCatalog()
    rows = _doctor_over(monkeypatch, cat, managed=True, rung_reads=[])
    assert cat.calls == 2, "one shared walk, retried once, then every row degrades without walking"
    for label in ("stale index-run fences", "tumbler allocator drift"):
        assert "skipped" in rows[label].detail, (label, rows[label].detail)


def test_a_local_engine_keeps_the_rung_scan_in_doctor(monkeypatch) -> None:
    """The refusal is the managed rule (auto_guard): a local engine's scan is cheap and doctor still runs it."""
    class LocalCatalog(CountingCatalog):
        def get_manifests(self, *_a, **_k):
            return {}

    cat = LocalCatalog(n=10)
    rung = ManifestSectionLabelsRung(
        reader_factory=lambda: cat, writer_factory=lambda: None, t3_factory=lambda: None,
        record_fn=lambda: None, memo_store=_NoMemo(), managed_fn=lambda: False,
    )
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))
    (row,) = health._check_pending_rungs()
    assert cat.calls == 1, "doctor on a local engine still runs the rung's dry-run scan"
    assert row.ok and "no pending rungs" in row.detail


@pytest.mark.parametrize("check", ["_check_stale_indexing_runs", "_check_failed_runs_hidden_chunks", "_check_next_seq_drift"])
def test_the_shared_walk_parameter_has_a_precise_type(check: str) -> None:
    """The three rows take the doctor run's one walk: a list of documents, the failed-walk marker, or None (called alone).
    It was annotated ``object``, which a type checker reads as 'anything'."""
    import typing

    hint = typing.get_type_hints(getattr(health, check))["documents"]
    assert {typing.get_origin(a) or a for a in typing.get_args(hint)} == {list, health._WalkFailed, type(None)}, hint
