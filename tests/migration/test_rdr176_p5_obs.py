# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 5 (Gap 5) — a running migration must be observable.

Failing-first (bead nexus-t9rmg.29). Today ``nx storage migrate all`` is silent
except on failure: the orchestrator fires ``on_store`` before each store but
emits NO per-store progress signal once it completes, and the destination-side
row counts that verification computes are discarded (only a verdict survives).
So "is it progressing? did rows actually land?" can only be answered by inferring
from OS CPU or paginating the read API.

This pins two observability contracts on ``orchestrator.migrate_all``:
  1. An ``on_progress(store, written, read)`` callback fires once per store as it
     completes, carrying that store's running written/read counts — so the CLI
     can surface per-store progress instead of silence.
  2. The report carries a ``dest_counts`` metric: the destination-side (pg) row
     counts verification reconciled, surfaced as a first-class field instead of
     thrown away.
"""
from __future__ import annotations

from nexus.migration import orchestrator as orch
from nexus.migration.etl_registry import EtlSources, StoreEtl


def _fake_etls(order_sink: list[str]):
    """Fake StoreEtls that feed the collector 10 read / 10 written per store."""

    def _runner(store: str):
        def run(sources: EtlSources, collector) -> dict:
            order_sink.append(store)
            collector.count_read(store, store, 10)
            collector.count_written(store, store, 10)
            return {}

        return run

    return [
        StoreEtl(s, _runner(s))
        for s in ("catalog", "memory", "chash", "plans", "taxonomy",
                  "telemetry", "aspects", "aspects_queue")
    ]


class _EchoCountSource:
    """Returns pg counts echoing the requested relations (all reconcile)."""

    def __init__(self) -> None:
        self.seen: list[str] | None = None

    def counts(self, relations: list[str]) -> dict[str, int]:
        self.seen = list(relations)
        return {r: 10 for r in relations}


def _sources(tmp_path) -> EtlSources:
    db = tmp_path / "memory.db"
    cat = tmp_path / ".catalog.db"
    db.touch()
    cat.touch()
    return EtlSources(sqlite_path=db, catalog_db_path=cat)


def test_migrate_all_emits_per_store_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
    progress: list[tuple[str, int, int]] = []

    orch.migrate_all(
        _sources(tmp_path),
        count_source=_EchoCountSource(),
        on_progress=lambda store, written, read: progress.append((store, written, read)),
    )

    # One progress signal per store, in ladder order, carrying its counts.
    stores = [p[0] for p in progress]
    assert stores == [
        "memory", "plans", "telemetry", "taxonomy",
        "aspects", "chash", "catalog", "aspects_queue",
    ]
    assert all(written == 10 and read == 10 for _s, written, read in progress)


def test_migrate_all_report_has_dest_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
    cs = _EchoCountSource()

    report = orch.migrate_all(_sources(tmp_path), count_source=cs)

    # The destination-side row counts verification reconciled are surfaced as a
    # first-class metric, not discarded.
    assert "dest_counts" in report
    assert report["dest_counts"]  # non-empty
    assert report["dest_counts"] == {r: 10 for r in (cs.seen or [])}


class _UnreachableCountSource:
    """A count source that cannot answer (service unreachable)."""

    def counts(self, relations: list[str]) -> None:
        return None


def test_dest_counts_empty_and_indeterminate_when_count_source_unreachable(
    tmp_path, monkeypatch,
) -> None:
    """When the count source is unreachable, dest_counts is {} AND verification
    is 'indeterminate' — the two must be read together so {} reads as
    'unavailable', never as 'zero rows landed' (code-review-expert, 2026-06-30)."""
    monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))

    report = orch.migrate_all(
        _sources(tmp_path), count_source=_UnreachableCountSource(),
    )

    assert report["verification"] == "indeterminate"
    assert report["dest_counts"] == {}


def test_no_progress_signal_for_crashed_store(tmp_path, monkeypatch) -> None:
    """on_progress is suppressed for a store that crashes — on_store_failed is
    the authoritative signal; a '0 written' progress line would misread as
    'completed empty'."""
    def _crashing_etls():
        def _runner(store: str):
            def run(sources: EtlSources, collector) -> dict:
                if store == "plans":
                    raise RuntimeError("mid-run partition")
                collector.count_read(store, store, 10)
                collector.count_written(store, store, 10)
                return {}
            return run
        return [
            StoreEtl(s, _runner(s))
            for s in ("memory", "plans", "telemetry", "taxonomy",
                      "aspects", "chash", "catalog", "aspects_queue")
        ]

    monkeypatch.setattr(orch, "build_store_etls", lambda s: _crashing_etls())
    progress: list[str] = []
    failed: list[str] = []
    orch.migrate_all(
        _sources(tmp_path),
        count_source=_EchoCountSource(),
        on_store_failed=lambda store, exc: failed.append(store),
        on_progress=lambda store, w, r: progress.append(store),
    )

    assert "plans" in failed              # crash surfaced via on_store_failed
    assert "plans" not in progress        # NOT also a progress line
    assert "memory" in progress           # healthy stores still report
