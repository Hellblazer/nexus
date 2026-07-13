# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gednd: taxonomy_assign_batch_hook loudness tripwire (RDR-172 pattern).

The assign hook is best-effort: it swallows its own exceptions, so
``HookRegistry.fire_batch`` sees success and records NO hook_failures row —
topic-scoped search went silently incomplete. The tripwire mirrors the
aspect-enqueue fix (aspect_worker.py): persist a structured hook_failures
row directly and log at warning, with the persist itself best-effort.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from nexus import mcp_infra


def _fire_local_path_failure(monkeypatch, captured: list) -> None:
    """Drive the LOCAL path into its exception handler with a capturing t2."""
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    monkeypatch.setattr(mcp_infra, "get_t3", lambda: MagicMock(spec=["_client"]))

    def _boom(*a, **k):
        raise RuntimeError("chroma exploded")

    monkeypatch.setattr(CatalogTaxonomy, "compute_assignments", _boom)

    def _capture_write(fn):
        t2 = MagicMock()
        fn(t2)
        captured.append(t2)

    monkeypatch.setattr(mcp_infra, "t2_index_write", _capture_write)
    mcp_infra.taxonomy_assign_batch_hook(
        ["doc1", "doc2"], "knowledge__tw__voyage-context-3__v1",
        ["c1", "c2"], [[0.1], [0.2]], None,
    )


def test_local_path_failure_records_hook_failures_row(monkeypatch):
    captured: list = []
    _fire_local_path_failure(monkeypatch, captured)  # must not raise

    assert captured, "tripwire must persist a hook_failures row via t2_index_write"
    call = captured[-1].telemetry.record_hook_failure.call_args
    assert call.kwargs["hook_name"] == "taxonomy_assign_batch_hook"
    assert call.kwargs["collection"] == "knowledge__tw__voyage-context-3__v1"
    assert call.kwargs["doc_id"] == "doc1"
    assert "RuntimeError" in call.kwargs["error"]


def test_tripwire_persist_failure_never_propagates(monkeypatch):
    """The tripwire's own persist is best-effort: a telemetry-write failure
    (T2 down, service 5xx) must never turn the best-effort hook fatal."""
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    monkeypatch.setattr(mcp_infra, "get_t3", lambda: MagicMock(spec=["_client"]))

    def _boom(*a, **k):
        raise RuntimeError("chroma exploded")

    def _t2_down(fn):
        raise ConnectionError("t2 unreachable")

    monkeypatch.setattr(CatalogTaxonomy, "compute_assignments", _boom)
    monkeypatch.setattr(mcp_infra, "t2_index_write", _t2_down)
    # Must not raise despite BOTH the hook body and the tripwire persist failing.
    mcp_infra.taxonomy_assign_batch_hook(
        ["doc1"], "knowledge__tw__voyage-context-3__v1", ["c1"], [[0.1]], None,
    )
