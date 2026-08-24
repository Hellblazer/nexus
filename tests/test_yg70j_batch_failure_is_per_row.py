# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A batch-extract failure must not be a verdict about every row in the batch.

nexus-yg70j defect A. `_process_batch`'s except arm called `mark_failed` --
terminal, "terminal until re-enqueued" -- on EVERY row, for ANY exception. The
single-row path already routed through `_mark_retry_or_fail_routed` (RDR-163
P1 / nexus-ztpt6), which has a real transient-vs-terminal taxonomy and a retry
budget. Only the batch path bypassed it.

MEASURED, on the 2026-08-24 incident's 26 terminally-failed rows:

     7  relative source_paths  -- genuine victims of the cwd fault
    19  absolute source_paths  -- ALL 19 EXIST ON DISK, nothing wrong with them

The 19 were healthy documents killed solely by sharing a batch of five with one
bad neighbour: 73% collateral. The batch is a transport detail; it is not a fact
about any document in it.

The noqa comment on that arm said "failure logged and rows re-queued" while the
code terminal-failed them. A reader checking whether failures were handled read
that and moved on, which is the likeliest reason it survived review -- so these
tests assert the OBSERVABLE routing, never the comment.
"""
from __future__ import annotations

import pytest


class _Row:
    def __init__(self, sp, retry_count=0):
        self.collection = "rdr__1-1__voyage-context-3__v1"
        self.source_path = sp
        self.content = "x"
        self.retry_count = retry_count
        self.doc_id = ""


@pytest.fixture
def worker(monkeypatch):
    from nexus import aspect_worker as m

    w = object.__new__(m.AspectExtractionWorker) if hasattr(m, "AspectExtractionWorker") else None
    if w is None:  # pragma: no cover — surfaces a rename rather than skipping quietly
        pytest.fail("AspectExtractionWorker not found in nexus.aspect_worker")
    return w


def test_batch_failure_routes_every_row_and_terminal_fails_none_wholesale(worker, monkeypatch):
    """The regression pin. Pre-fix this recorded 5 terminal failures."""
    from nexus import aspect_worker as m

    routed, terminal = [], []
    monkeypatch.setattr(
        m.AspectExtractionWorker, "_mark_retry_or_fail_routed",
        lambda self, row, exc: routed.append(row.source_path), raising=True,
    )
    monkeypatch.setattr(
        m.AspectExtractionWorker, "_mark_failed_routed",
        lambda self, row, err: terminal.append(row.source_path), raising=True,
    )
    monkeypatch.setattr(m, "_extract_aspects_batch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("batch boom")))
    import nexus.mcp_infra as _mi
    monkeypatch.setattr(_mi, "t2_index_write", lambda fn: None, raising=False)
    import nexus.commands.enrich as _en
    monkeypatch.setattr(_en, "_build_catalog_manifest_lookup", lambda: None, raising=False)

    rows = [_Row("docs/rdr/relative.md")] + [
        _Row(f"/Users/example/git/nexus/docs/rdr/healthy-{i}.md") for i in range(4)
    ]
    worker._process_batch(rows)

    assert routed == [r.source_path for r in rows], (
        "every row must reach the router that decides retry-vs-terminal per row; "
        f"got {routed}"
    )
    assert terminal == [], (
        "the batch arm must not terminal-fail rows wholesale — that is the 73% "
        f"collateral this bead measured; got {terminal}"
    )


def test_the_four_healthy_neighbours_are_not_judged_by_the_bad_one(worker, monkeypatch):
    """States the incident shape directly: one bad row, four good ones."""
    from nexus import aspect_worker as m

    seen = {}
    def _route(self, row, exc):
        seen[row.source_path] = type(exc).__name__
    monkeypatch.setattr(m.AspectExtractionWorker, "_mark_retry_or_fail_routed", _route, raising=True)
    monkeypatch.setattr(m.AspectExtractionWorker, "_mark_failed_routed",
                        lambda self, row, err: pytest.fail(f"terminal-failed {row.source_path}"),
                        raising=True)
    monkeypatch.setattr(m, "_extract_aspects_batch",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError(2, "No such file")))
    import nexus.mcp_infra as _mi
    monkeypatch.setattr(_mi, "t2_index_write", lambda fn: None, raising=False)
    import nexus.commands.enrich as _en
    monkeypatch.setattr(_en, "_build_catalog_manifest_lookup", lambda: None, raising=False)

    healthy = [f"/Users/example/git/nexus/docs/rdr/healthy-{i}.md" for i in range(4)]
    worker._process_batch([_Row("docs/rdr/relative.md"), *[_Row(h) for h in healthy]])

    for h in healthy:
        assert h in seen, f"healthy neighbour {h} never reached the router"


def test_a_routing_error_on_one_row_does_not_abandon_the_rest(worker, monkeypatch):
    """Non-vacuity on the error path: the per-row try/except must continue."""
    from nexus import aspect_worker as m

    routed = []
    def _route(self, row, exc):
        if row.source_path.endswith("boom.md"):
            raise RuntimeError("routing persist failed")
        routed.append(row.source_path)
    monkeypatch.setattr(m.AspectExtractionWorker, "_mark_retry_or_fail_routed", _route, raising=True)
    monkeypatch.setattr(m, "_extract_aspects_batch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    import nexus.mcp_infra as _mi
    monkeypatch.setattr(_mi, "t2_index_write", lambda fn: None, raising=False)
    import nexus.commands.enrich as _en
    monkeypatch.setattr(_en, "_build_catalog_manifest_lookup", lambda: None, raising=False)

    worker._process_batch([_Row("a.md"), _Row("boom.md"), _Row("c.md")])
    assert routed == ["a.md", "c.md"], f"a bad row aborted the sweep; got {routed}"
