# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gednd: taxonomy_assign_batch_hook loudness tripwire (RDR-172 pattern).

The assign hook is best-effort: it swallows its own exceptions, so
``HookRegistry.fire_batch`` sees success and records NO hook_failures row —
topic-scoped search went silently incomplete. The tripwire mirrors the
aspect-enqueue fix (aspect_worker.py): persist a structured hook_failures
row directly and log at warning, with the persist itself best-effort.

PORTED to the service path (nexus-i711w Stage 2 sub-stage C). These tests used
to drive the hook's LOCAL path into its handler by making
``CatalogTaxonomy.compute_assignments`` raise. That path is deleted, but the
CONTRACT is not: the hook still swallows, and it still has to leave a
hook_failures row behind. The surviving service arm has its own ``except``
doing exactly that, so the DRIVER moved and the subject did not. Deleting these
with the branch would have retired the only proof that a swallowed taxonomy
failure stays visible.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from nexus import mcp_infra


def _force_service_path(monkeypatch) -> None:
    """Make the hook take its service arm.

    The guard is instance-based (nexus-1k8s1) and resolved through a deferred
    import inside the hook, so patch the function on its defining module.
    """
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: MagicMock())
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_service_backed", lambda _t3: True
    )


def _fire_service_path_failure(monkeypatch, captured: list) -> None:
    """Drive the SERVICE path into its exception handler with a capturing t2."""
    _force_service_path(monkeypatch)

    def _capture_write(fn, **_kwargs):
        t2 = MagicMock()
        captured.append(t2)
        # nexus-yu9w5: the service arm calls assign_from_chashes (the engine
        # route) INSIDE the t2_index_write lambda, so raising there reaches
        # the same handler the old compute_assignments driver did. The
        # tripwire's own persist then arrives as a later call.
        t2.taxonomy.assign_from_chashes.side_effect = RuntimeError("service exploded")
        fn(t2)

    monkeypatch.setattr(mcp_infra, "t2_index_write", _capture_write)
    mcp_infra.taxonomy_assign_batch_hook(
        ["doc1", "doc2"], "knowledge__tw__voyage-context-3__v1",
        ["c1", "c2"], [[0.1], [0.2]], None,
    )


def test_service_path_failure_records_hook_failures_row(monkeypatch):
    captured: list = []
    _fire_service_path_failure(monkeypatch, captured)  # must not raise

    assert captured, "tripwire must persist a hook_failures row via t2_index_write"
    recording = [t2 for t2 in captured if t2.telemetry.record_hook_failure.called]
    assert recording, (
        "a swallowed taxonomy failure recorded no hook_failures row — the "
        "silent-failure class this tripwire exists to prevent"
    )
    call = recording[-1].telemetry.record_hook_failure.call_args
    assert call.kwargs["hook_name"] == "taxonomy_assign_batch_hook"
    assert call.kwargs["collection"] == "knowledge__tw__voyage-context-3__v1"
    assert call.kwargs["doc_id"] == "doc1"
    assert "RuntimeError" in call.kwargs["error"]


def test_tripwire_persist_failure_never_propagates(monkeypatch):
    """The tripwire's own persist is best-effort: a telemetry-write failure
    (T2 down, service 5xx) must never turn the best-effort hook fatal."""
    _force_service_path(monkeypatch)

    def _t2_down(fn, **_kwargs):
        raise ConnectionError("t2 unreachable")

    monkeypatch.setattr(mcp_infra, "t2_index_write", _t2_down)
    # Must not raise despite BOTH the hook body and the tripwire persist failing.
    mcp_infra.taxonomy_assign_batch_hook(
        ["doc1"], "knowledge__tw__voyage-context-3__v1", ["c1"], [[0.1]], None,
    )
